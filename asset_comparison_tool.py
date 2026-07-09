"""Asset comparison tool (GUI).

Define a Basket and a Benchmark portfolio of Yahoo Finance tickers or ISINs
with weights, choose a base currency and an optional start date. The tool
downloads max history (dividend-adjusted), converts everything to the base
currency and plots the normalized total-return comparison of the two
portfolios plus their yearly return difference. The chart is shown in a
window and also saved as asset_comparison.png.
"""

import difflib
import queue
import re
import threading
import tkinter as tk
import traceback
import unicodedata
from tkinter import messagebox, scrolledtext, ttk

import matplotlib
matplotlib.use("TkAgg")

import matplotlib.ticker as mticker
import pandas as pd
import requests
import yfinance as yf
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

N_ROWS = 10


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------

def to_date_index(s):
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = s.copy()
    out.index = idx.normalize()
    return out


_fx_cache = {}


def fx_to_target(src, target):
    if src == target or not src or src in ("NONE", "None"):
        return None
    key = (src, target)
    if key not in _fx_cache:
        fx = yf.Ticker(f"{src}{target}=X").history(period="max", auto_adjust=True)["Close"]
        if fx.empty:
            raise ValueError(f"No FX data for {src}{target}=X")
        _fx_cache[key] = to_date_index(fx)
    return _fx_cache[key]


ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def resolve_isin(isin, log=print):
    """Resolve an ISIN to a Yahoo Finance ticker via Yahoo's search API."""
    try:
        quotes = yf.Search(isin, max_results=10).quotes
    except Exception:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": isin}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        r.raise_for_status()
        quotes = r.json().get("quotes", [])
    symbols = [q["symbol"] for q in quotes if q.get("symbol")]
    if not symbols:
        raise ValueError(f"Yahoo Finance knows no listing for ISIN {isin}")
    if len(symbols) > 1:
        log(f"  {isin}: several listings found ({', '.join(symbols)}), using {symbols[0]}")
    return symbols[0]


def isin_from_onvista(long_name, short_name):
    """Look up the ISIN by fund/company name via onvista's public search.

    Only returns an ISIN when exactly one candidate remains after matching,
    so an ambiguous match (e.g. Acc vs Dist share class) yields None rather
    than a possibly wrong ISIN.
    """
    def norm(s):
        s = unicodedata.normalize("NFKD", s)
        return "".join(ch for ch in s.lower() if ch.isalnum())

    target = norm(long_name)
    hints = f"{long_name} {short_name}".lower()
    words = long_name.split()
    # onvista's search finds nothing for overly long queries, so fall back
    # to shorter prefixes of the name
    queries = []
    for q in (" ".join(words[:6]), " ".join(words[:2]), words[0]):
        if q and q not in queries:
            queries.append(q)

    for q in queries:
        r = requests.get(
            "https://api.onvista.de/api/v1/instruments/query",
            params={"limit": 20, "searchValue": q},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        r.raise_for_status()
        scores, names = {}, {}
        receipts = {"adr", "cdr", "gdr"}
        hint_tokens = set("".join(ch if ch.isalnum() else " " for ch in hints).split())
        for c in r.json().get("list", []):
            if c.get("entityType") not in ("STOCK", "FUND") or not c.get("isin"):
                continue
            cname = c.get("name", "")
            n = norm(cname)
            if not n:
                continue
            # skip depositary receipts (ADR/CDR/GDR) unless the asset is one
            ctokens = set("".join(ch if ch.isalnum() else " " for ch in cname.lower()).split())
            if ctokens & receipts and not (hint_tokens & receipts):
                continue
            # prefix match in either direction tolerates abbreviations like
            # "USD Acc." vs "USD Accumulation"; the fuzzy ratio additionally
            # tolerates typos and reordered words in onvista's names
            if n.startswith(target) or target.startswith(n):
                score = 1.0
            else:
                score = difflib.SequenceMatcher(None, target, n).ratio()
            if score >= 0.75 and score > scores.get(c["isin"], 0):
                scores[c["isin"]] = score
                names[c["isin"]] = c["name"]
        cands = list(scores)
        if len(cands) > 1:
            # try to pick the share class using hints from the Yahoo names
            if "acc" in hints:
                cands = [i for i in cands if "acc" in names[i].lower()] or cands
            elif "dis" in hints:
                cands = [i for i in cands if "dis" in names[i].lower()] or cands
        cands.sort(key=lambda i: scores[i], reverse=True)
        if len(cands) == 1:
            return cands[0]
        # accept an ambiguous result only if the best match is clearly ahead
        if len(cands) > 1 and scores[cands[0]] - scores[cands[1]] >= 0.05:
            return cands[0]
    return None


def get_meta(tk_, ticker, known_isin=None):
    long_name = short_name = None
    try:
        info = tk_.info
        long_name = info.get("longName")
        short_name = info.get("shortName")
    except Exception:
        pass
    name = long_name or short_name
    isin = known_isin
    if isin is None:
        try:
            isin = tk_.isin
            if isin in (None, "", "-"):
                isin = None
        except Exception:
            pass
    if isin is None and name:
        # yfinance's ISIN lookup often fails for non-US listings
        try:
            isin = isin_from_onvista(name, short_name or "")
        except Exception:
            pass
    return {"name": name or ticker, "isin": isin}


def asset_label(t, meta):
    m = meta.get(t, {})
    parts = [t]
    if m.get("isin"):
        parts.append(m["isin"])
    return f"{m.get('name', t)} ({', '.join(parts)})"


def load_asset(ticker, target, known_isin, log):
    tk_ = yf.Ticker(ticker)
    hist = tk_.history(period="max", auto_adjust=True)
    if hist.empty:
        raise ValueError(f"No price data for {ticker}")
    close = to_date_index(hist["Close"])
    currency = tk_.fast_info["currency"]
    fx = fx_to_target(currency, target)
    if fx is not None:
        close = (close * fx.reindex(close.index, method="ffill")).dropna()
    meta = get_meta(tk_, ticker, known_isin)
    log(f"  {ticker}: {meta['name']} (ISIN: {meta['isin'] or 'n/a'})")
    log(f"  {ticker}: {close.index[0].date()} to {close.index[-1].date()} "
        f"({len(close)} days, {currency}->{target})")
    return close, meta


def build_result(portfolios, target, start, log):
    """portfolios: {name: [(ticker_or_isin, weight_percent), ...]}

    Returns (df, weights, metas) where df holds one total-return series per
    portfolio, normalized to 100 at the start of the common date range.
    """
    prices, metas, weights = {}, {}, {}
    for pname, rows in portfolios.items():
        w = {}
        for entry, weight in rows:
            known_isin = None
            t = entry
            if ISIN_RE.match(entry):
                known_isin = entry
                t = resolve_isin(entry, log)
                log(f"  ISIN {entry} -> ticker {t}")
            if t not in prices:
                log(f"Downloading {t} ...")
                prices[t], metas[t] = load_asset(t, target, known_isin, log)
            w[t] = w.get(t, 0.0) + weight
        weights[pname] = w

    df = pd.DataFrame(prices).dropna()
    if df.empty:
        raise ValueError("No overlapping date range between all assets.")
    if start is not None:
        df = df.loc[df.index >= start]
        if len(df) < 2:
            raise ValueError(f"No overlapping data on or after {start.date()}.")

    # buy-and-hold portfolios: initial weights at the first day of the
    # comparison period, no rebalancing
    norm = df / df.iloc[0]
    series = {}
    for pname, w in weights.items():
        total = sum(w.values())
        series[pname] = sum(norm[t] * (wt / total) for t, wt in w.items()) * 100
    out = pd.DataFrame(series)
    log(f"Comparison period: {out.index[0].date()} to {out.index[-1].date()}")
    return out, weights, metas


# --------------------------------------------------------------------------
# Chart
# --------------------------------------------------------------------------

def portfolio_label(pname, w, metas, cagr):
    total = sum(w.values())
    lines = [f"{pname}  —  CAGR: {cagr:.2%}"]
    for t, wt in w.items():
        lines.append(f"      {wt / total:6.1%}  {asset_label(t, metas)}")
    return "\n".join(lines)


def make_figure(df, weights, metas, target):
    cols = list(df.columns)
    years = (df.index[-1] - df.index[0]).days / 365.25
    cagrs = {c: (df[c].iloc[-1] / df[c].iloc[0]) ** (1 / years) - 1 for c in cols}

    yearly_diff = None
    if len(cols) == 2:
        yearly = df.resample("YE").last().pct_change().dropna()
        yearly.index = yearly.index.year
        if len(yearly) >= 2:
            yearly_diff = yearly[cols[0]] - yearly[cols[1]]

    two = yearly_diff is not None
    fig = Figure(figsize=(12.5, 8.5) if two else (12.5, 6), dpi=100)
    if two:
        ax1, ax2 = fig.subplots(2, 1, gridspec_kw={"height_ratios": [2, 1]})
    else:
        ax1 = fig.subplots()

    for c in cols:
        ax1.plot(df.index, df[c],
                 label=portfolio_label(c, weights[c], metas, cagrs[c]), linewidth=1.5)
    ax1.set_title(f"{' vs '.join(cols)} — Total Return Comparison ({target}, normalized to 100)",
                  fontsize=13)
    ax1.set_ylabel(f"Growth of {target} 100")
    ax1.legend(fontsize=9)
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(mticker.FormatStrFormatter(f"{target} %.0f"))

    if two:
        colors = ["green" if v >= 0 else "red" for v in yearly_diff.values]
        ax2.bar(yearly_diff.index, yearly_diff.values * 100, color=colors, alpha=0.7)
        ax2.axhline(0, color="black", linewidth=0.8)
        ax2.set_title(f"Yearly Return Difference ({cols[0]} − {cols[1]})", fontsize=13)
        ax2.set_ylabel("Difference (pp)")
        ax2.set_xlabel("Year")
        ax2.grid(True, alpha=0.3, axis="y")
        ax2.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        ax2.margins(y=0.15)
        for x, v in zip(yearly_diff.index, yearly_diff.values * 100):
            ax2.text(x, v + (0.15 if v >= 0 else -0.35), f"{v:+.2f}",
                     ha="center", va="bottom" if v >= 0 else "top", fontsize=7)

    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class PortfolioTable(ttk.LabelFrame):
    def __init__(self, master, title):
        super().__init__(master, text=title)
        ttk.Label(self, text="Ticker / ISIN").grid(row=0, column=0, padx=4, pady=(2, 4))
        ttk.Label(self, text="Weight %").grid(row=0, column=1, padx=4, pady=(2, 4))
        self.symbols, self.weights = [], []
        for i in range(N_ROWS):
            s = ttk.Entry(self, width=24)
            w = ttk.Entry(self, width=8, justify="right")
            s.grid(row=i + 1, column=0, padx=4, pady=1)
            w.grid(row=i + 1, column=1, padx=4, pady=1)
            self.symbols.append(s)
            self.weights.append(w)
        ttk.Button(self, text="Equally weight", command=self.equally_weight).grid(
            row=N_ROWS + 1, column=0, columnspan=2, pady=(6, 4))

    def equally_weight(self):
        filled = [i for i, s in enumerate(self.symbols) if s.get().strip()]
        if not filled:
            return
        w = round(100.0 / len(filled), 2)
        for i in range(N_ROWS):
            self.weights[i].delete(0, tk.END)
            if i in filled:
                self.weights[i].insert(0, f"{w:g}")

    def parse(self):
        """Return [(symbol, weight_percent), ...]; raises ValueError."""
        title = self.cget("text")
        rows = []
        for s, w in zip(self.symbols, self.weights):
            sym = s.get().strip().upper()
            if not sym:
                continue
            wtxt = w.get().strip().replace("%", "").replace(",", ".")
            try:
                weight = float(wtxt)
            except ValueError:
                raise ValueError(f"{title}: missing or invalid weight for {sym}.")
            if weight <= 0:
                raise ValueError(f"{title}: weight for {sym} must be positive.")
            rows.append((sym, weight))
        if not rows:
            raise ValueError(f"{title}: please enter at least one asset.")
        total = sum(w for _, w in rows)
        if not 99.0 <= total <= 101.0:
            raise ValueError(f"{title}: weights sum to {total:g}%, "
                             f"they must sum to 100% (99-101 allowed for rounding).")
        return rows


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Asset Comparison Tool (Yahoo Finance)")

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="Base currency:").grid(row=0, column=0, sticky="w")
        self.currency = ttk.Entry(top, width=8)
        self.currency.insert(0, "CHF")
        self.currency.grid(row=0, column=1, padx=(4, 24), sticky="w")
        ttk.Label(top, text="Start date (YYYY-MM-DD, 0 = longest possible):").grid(
            row=0, column=2, sticky="w")
        self.start = ttk.Entry(top, width=12)
        self.start.insert(0, "0")
        self.start.grid(row=0, column=3, padx=4, sticky="w")

        tables = ttk.Frame(self.root)
        tables.pack(padx=10, pady=4)
        self.basket = PortfolioTable(tables, "Basket")
        self.benchmark = PortfolioTable(tables, "Benchmark")
        self.basket.grid(row=0, column=0, padx=(0, 12), sticky="n")
        self.benchmark.grid(row=0, column=1, sticky="n")

        self.compare_btn = ttk.Button(self.root, text="Compare", command=self.compare)
        self.compare_btn.pack(pady=6)

        self.log_box = scrolledtext.ScrolledText(self.root, height=10, width=80,
                                                 state="disabled")
        self.log_box.pack(padx=10, pady=(0, 10), fill="both", expand=True)

        self.queue = queue.Queue()
        self.root.after(100, self._poll)

    def log(self, msg):
        self.queue.put(("log", msg))

    def _append_log(self, msg):
        self.log_box.config(state="normal")
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state="disabled")

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.config(state="disabled")

    def compare(self):
        try:
            target = self.currency.get().strip().upper()
            if len(target) != 3 or not target.isalpha():
                raise ValueError("Please enter a 3-letter base currency (e.g. CHF).")
            raw = self.start.get().strip()
            start = None
            if raw not in ("", "0"):
                try:
                    start = pd.Timestamp(raw).normalize()
                except Exception:
                    raise ValueError("Invalid start date, use YYYY-MM-DD "
                                     "(or 0 for the longest possible period).")
            portfolios = {"Basket": self.basket.parse(),
                          "Benchmark": self.benchmark.parse()}
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return
        self.compare_btn.config(state="disabled")
        self._clear_log()
        threading.Thread(target=self._worker, args=(portfolios, target, start),
                         daemon=True).start()

    def _worker(self, portfolios, target, start):
        try:
            result = build_result(portfolios, target, start, self.log)
            self.queue.put(("done", (result, target)))
        except Exception as e:
            self.queue.put(("error", str(e) or traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "error":
                    self._append_log(f"ERROR: {payload}")
                    self.compare_btn.config(state="normal")
                    messagebox.showerror("Error", payload)
                elif kind == "done":
                    (df, weights, metas), target = payload
                    fig = make_figure(df, weights, metas, target)
                    try:
                        fig.savefig("asset_comparison.png", dpi=150)
                        self._append_log("Saved chart to asset_comparison.png")
                    except Exception as e:
                        self._append_log(f"WARNING: could not save PNG: {e}")
                    self._show_result(fig)
                    self.compare_btn.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _show_result(self, fig):
        win = tk.Toplevel(self.root)
        win.title("Comparison result")
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        NavigationToolbar2Tk(canvas, win)
        canvas.get_tk_widget().pack(fill="both", expand=True)


def main():
    App().root.mainloop()


if __name__ == "__main__":
    main()

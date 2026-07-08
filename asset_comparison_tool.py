"""Interactive asset comparison tool.

Asks for Yahoo Finance tickers or ISINs and a base currency, downloads max
history (dividend-adjusted), converts everything to the base currency and
plots a normalized total-return comparison. With exactly two assets it also
shows the yearly return difference as a bar chart.
"""

import difflib
import re
import sys
import traceback
import unicodedata

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import requests
import yfinance as yf


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
    if src not in _fx_cache:
        fx = yf.Ticker(f"{src}{target}=X").history(period="max", auto_adjust=True)["Close"]
        if fx.empty:
            raise ValueError(f"No FX data for {src}{target}=X")
        _fx_cache[src] = to_date_index(fx)
    return _fx_cache[src]


ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def resolve_isin(isin):
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
        print(f"  {isin}: several listings found ({', '.join(symbols)}), using {symbols[0]}")
    return symbols[0]


def ask_tickers():
    while True:
        raw = input("Yahoo Finance tickers or ISINs, separated by comma or space (e.g. ADWI.SW, IE00B3RBWM25): ").strip()
        tickers = [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]
        if len(tickers) >= 2:
            return tickers
        print("Please enter at least two tickers or ISINs.\n")


def ask_currency():
    raw = input("Base currency [CHF]: ").strip().upper()
    return raw if raw else "CHF"


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


def get_meta(tk, ticker, known_isin=None):
    long_name = short_name = None
    try:
        info = tk.info
        long_name = info.get("longName")
        short_name = info.get("shortName")
    except Exception:
        pass
    name = long_name or short_name
    isin = known_isin
    if isin is None:
        try:
            isin = tk.isin
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


def download(tickers, target):
    data = {}
    meta = {}
    for entry in tickers:
        print(f"Downloading {entry} ...")
        try:
            known_isin = None
            t = entry
            if ISIN_RE.match(entry):
                known_isin = entry
                t = resolve_isin(entry)
                print(f"  ISIN {entry} -> ticker {t}")
            if t in data:
                print(f"  {t} already loaded, skipping duplicate.")
                continue
            tk = yf.Ticker(t)
            hist = tk.history(period="max", auto_adjust=True)
            if hist.empty:
                print(f"  WARNING: no data for {t}, skipping.")
                continue
            close = to_date_index(hist["Close"])
            currency = tk.fast_info["currency"]
            fx = fx_to_target(currency, target)
            if fx is not None:
                close = close * fx.reindex(close.index, method="ffill")
                close = close.dropna()
            data[t] = close
            meta[t] = get_meta(tk, t, known_isin)
            print(f"  {t}: {meta[t]['name']} (ISIN: {meta[t]['isin'] or 'n/a'})")
            print(f"  {t}: {close.index[0].date()} to {close.index[-1].date()} "
                  f"({len(close)} days, {currency}->{target})")
        except Exception as e:
            print(f"  WARNING: failed to load {entry}: {e}")
    return data, meta


def plot(df, tickers, target, meta):
    normalized = df / df.iloc[0] * 100

    years = (df.index[-1] - df.index[0]).days / 365.25
    cagrs = {t: (df[t].iloc[-1] / df[t].iloc[0]) ** (1 / years) - 1 for t in tickers}

    yearly_diff = None
    if len(tickers) == 2:
        yearly_returns = df.resample("YE").last().pct_change().dropna()
        yearly_returns.index = yearly_returns.index.year
        if len(yearly_returns) >= 2:
            yearly_diff = yearly_returns[tickers[0]] - yearly_returns[tickers[1]]

    two = yearly_diff is not None
    if two:
        fig, axes = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [2, 1]})
        ax1 = axes[0]
    else:
        fig, ax1 = plt.subplots(figsize=(14, 7))

    for t in tickers:
        ax1.plot(normalized.index, normalized[t],
                 label=f"{asset_label(t, meta)}  —  CAGR: {cagrs[t]:.2%}", linewidth=1.5)
    ax1.set_title(f"{' vs '.join(tickers)} — Total Return Comparison ({target}, normalized to 100)", fontsize=14)
    ax1.set_ylabel(f"Growth of {target} 100")
    ax1.legend(fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.yaxis.set_major_formatter(mticker.FormatStrFormatter(f"{target} %.0f"))

    if two:
        ax2 = axes[1]
        colors = ["green" if v >= 0 else "red" for v in yearly_diff.values]
        ax2.bar(yearly_diff.index, yearly_diff.values * 100, color=colors, alpha=0.7)
        ax2.axhline(0, color="black", linewidth=0.8)
        ax2.set_title(f"Yearly Return Difference ({tickers[0]} − {tickers[1]})", fontsize=14)
        ax2.set_ylabel("Difference (pp)")
        ax2.set_xlabel("Year")
        ax2.grid(True, alpha=0.3, axis="y")
        ax2.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
        for x, v in zip(yearly_diff.index, yearly_diff.values * 100):
            ax2.text(x, v + (0.15 if v >= 0 else -0.35), f"{v:+.2f}",
                     ha="center", va="bottom" if v >= 0 else "top", fontsize=7)

    plt.tight_layout()
    out = "asset_comparison.png"
    plt.savefig(out, dpi=150)
    print(f"\nSaved chart to {out}")
    plt.show()


def main():
    print("=" * 60)
    print("  Asset Comparison Tool (Yahoo Finance)")
    print("=" * 60)
    while True:
        tickers = ask_tickers()
        target = ask_currency()
        print()
        data, meta = download(tickers, target)
        found = list(data)
        if len(found) < 2:
            print("\nNeed at least two assets with data. Please try again.\n")
            continue
        df = pd.DataFrame({t: data[t] for t in found}).dropna()
        if df.empty:
            print("\nNo overlapping date range between these assets. Please try again.\n")
            continue
        plot(df, found, target, meta)
        again = input("\nCompare more assets? [y/N]: ").strip().lower()
        if again not in ("y", "yes", "j", "ja"):
            break


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        pass
    except Exception:
        traceback.print_exc()
        input("\nAn error occurred. Press Enter to close...")
        sys.exit(1)

"""Interactive asset comparison tool.

Asks for Yahoo Finance tickers and a base currency, downloads max history
(dividend-adjusted), converts everything to the base currency and plots a
normalized total-return comparison. With exactly two tickers it also shows
the yearly return difference as a bar chart.
"""

import sys
import traceback

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
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


def ask_tickers():
    while True:
        raw = input("Yahoo Finance tickers, separated by comma or space (e.g. ADWI.SW, VT): ").strip()
        tickers = [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]
        if len(tickers) >= 2:
            return tickers
        print("Please enter at least two tickers.\n")


def ask_currency():
    raw = input("Base currency [CHF]: ").strip().upper()
    return raw if raw else "CHF"


def download(tickers, target):
    data = {}
    for t in tickers:
        print(f"Downloading {t} ...")
        try:
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
            print(f"  {t}: {close.index[0].date()} to {close.index[-1].date()} "
                  f"({len(close)} days, {currency}->{target})")
        except Exception as e:
            print(f"  WARNING: failed to load {t}: {e}")
    return data


def plot(df, tickers, target):
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
        ax1.plot(normalized.index, normalized[t], label=f"{t}  (CAGR: {cagrs[t]:.2%})", linewidth=1.5)
    ax1.set_title(f"{' vs '.join(tickers)} — Total Return Comparison ({target}, normalized to 100)", fontsize=14)
    ax1.set_ylabel(f"Growth of {target} 100")
    ax1.legend(fontsize=12)
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
        data = download(tickers, target)
        found = [t for t in tickers if t in data]
        if len(found) < 2:
            print("\nNeed at least two tickers with data. Please try again.\n")
            continue
        df = pd.DataFrame({t: data[t] for t in found}).dropna()
        if df.empty:
            print("\nNo overlapping date range between these assets. Please try again.\n")
            continue
        plot(df, found, target)
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

# Stock agent

A Python 3.11+ daily-close research monitor for your Canadian/US holdings and BTC-CAD. It generates separate **SWING** and **LONG_TERM** signals, ranks technical strength and historical price risk, screens a configurable list of additional stocks, and sends Telegram alerts. No Python packages are required.

**Status:** Telegram is configured locally and a daily noon Toronto-time schedule is active in the app. Credentials remain local. It does not place orders. This is an unbacktested technical strategy, not a forecast or a promise of profitable timing.

## Run an analysis

```sh
cd /Users/fatemehsadeghi/Documents/ChatGPT/stock
python3 -m stock_agent scan
```

Open `reports/latest.md` for readable analysis or `reports/latest.json` for structured data. Prices include their quote currency, provider identity and last completed daily-bar timestamp. Reports are ignored by Git.

All 16 requested tickers are recorded in `config.json` and marked as holdings. `SSPM.TP` is unresolved and disabled pending its full investment name and exchange. No ticker is silently replaced by its US equivalent. Unavailable provider symbols and insufficient history are reported as data issues. The discovery list is a small editable universe of Canadian/US companies, not an exhaustive market search.

## Create your Telegram bot

1. Open Telegram and find the official **@BotFather** account, or visit https://t.me/BotFather.
2. Send `/newbot`. Choose a display name, then a unique username ending in `bot`.
3. BotFather gives you a secret API token. Keep it private; do not paste it into chat, a GitHub issue or source code.
4. Open the new bot's chat and send `/start`. For group alerts, add the bot to that group and send a command addressed to it, such as `/start@YourBotUsername`.
5. Run the setup command below. It asks for the token using a hidden prompt, displays chats that recently messaged your bot, and asks you to explicitly choose the destination chat ID. Enter the number after `Chat` for your intended destination. If no chats appear, send `/start` to your new bot (not BotFather), then press Enter in Terminal to refresh without reentering your token. Enter `q` to cancel. An existing webhook can prevent `getUpdates`; use a new dedicated bot or manage that webhook yourself.

```sh
python3 -m stock_agent setup-telegram
python3 -m stock_agent test-telegram
python3 -m stock_agent scan --notify
```

Credentials are stored only in a local `.env` with owner-only permissions, ignored by Git. You can alternatively supply `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables. The setup command saves credentials; the test command sends a message to your selected chat. Do not share `.env`.

## One brief daily notification

The app's **Daily noon stock summary** schedule runs `python3 -m stock_agent scan --notify` every day at 12:00 noon in America/Toronto, starting November 2, 2026. Keep the app and this project available on an awake, online computer for local execution. The old detailed-alert watcher was stopped. You do not need to keep a separate Terminal watcher running for this app schedule.

The program analyses the configured holdings and discovery universe, saves the detailed report locally, and sends one short Telegram message. Examples:

```text
No buys or sells recommended.
```

```text
Daily-price signals
Swing: Buy XEG.TO; Sell NVDA.
Long-term: Buy TD.TO.
```

Those examples illustrate formatting, not current recommendations. Only actionable symbols appear. Sell actions are limited to configured holdings. Conflicting buy/sell timeframes are withheld and briefly marked as mixed signals. A short incomplete-data warning replaces any implication that unavailable data was successfully analysed. No separate routine news, unusual-activity or stock-status messages are sent.

`daily_notification` in `config.json` sets the delivery timezone, noon target and one-hour delivery/retry window. `scan --notify` sends only within that window and only once per local date. Outside the window, it generates the report without sending Telegram. SQLite remembers successful delivery across restarts; failed sends remain retryable. Keep `runtime/` to preserve this history. A crash or uncertain network response after Telegram accepts a message but before SQLite records it can still cause a duplicate.

The analysis uses **completed daily candles**, not live noon prices. Equity bars become eligible after 17:00 in the exchange timezone; crypto bars after 00:00 UTC. Technical rules do not incorporate every fundamental or news event. Recent provider-associated headlines are saved for review, not automatically scored as buy/sell causes.

For running independently of the app schedule, the optional Terminal watcher also respects the daily delivery window:

```sh
python3 -m stock_agent watch
```

It checks the schedule every 15 minutes and scans only when the daily summary is due. Keep that Terminal process running if you choose this alternative. Do not run it alongside the app automation or another notifying scan. A process lock blocks multiple watchers. There is no external uptime watchdog.

## How the signals work

| Output | Meaning |
|---|---|
| SWING BUY_CANDIDATE | Close above previous 20 closes; strength ≥80; RSI 50–70; positive benchmark-relative return; ATR within configured ceiling |
| SWING EXIT_REVIEW | Below 200-day average, or below both previous 20 closes and 50-day average |
| LONG_TERM BUY_CANDIDATE | Close above 200-day average; 50-day average above 200 and rising; RSI 45–65; price no more than 15% above 200-day average; positive relative return; ATR within ceiling |
| LONG_TERM EXIT_REVIEW | Close and 50-day average both below 200-day average |
| WAIT | No entry/exit condition; not a command to sell or a guarantee of safety |
| DATA_STALE | Data exceeds 4 calendar days for equities or 2 for crypto; no trading/activity alerts |
| Unusual activity | Absolute daily adjusted move ≥5% or volume ≥2.5× previous 20-day average; investigate cause, not an automatic buy/sell |

Swing and long-term conditions may disagree because they answer different questions. The long-term horizon still uses technical rules; it is not a valuation model.

Strength is the percentage of six rules satisfied: price above SMA50, price above SMA200, SMA50 above SMA200, rising SMA50, RSI 50–70, and positive 63 shared-session return relative to XEQT.TO. It is **not a confidence probability**. Benchmark returns use shared dates and native quote currencies; USD stocks are not converted into CAD. The score is a trend/momentum summary, not business quality or financial strength.

Risk labels derive from 63-return annualized historical volatility: below 20% LOWER, 20–35% MODERATE, 35–60% HIGH, ≥60% VERY HIGH; BTC is always VERY HIGH. LOWER does not mean safe. The report also shows 14-day Wilder ATR and an illustrative close-minus-2×ATR invalidation. Stops can gap; this is not a personalized stop-loss or position size. Indicators use adjusted OHLC so distributions and splits do not mechanically create signals. Reported close and invalidation are converted back to current quoted units.

## Portfolio context and limits

Your list combines broad equities, technology/semiconductors, energy, metals/miners and Bitcoin. Multiple ETFs can hold the same companies, so owning many tickers does not guarantee diversification. Holdings and position sizes are not retrieved from a broker. Before sizing trades, add cost basis, position values, cash, investing goals and acceptable loss; this version deliberately does not invent those values.

`config.json` contains qualitative risk notes for each instrument. Fundamental valuation, financial statements, earnings calendars, trust NAV discounts, ETF holdings overlap, macroeconomic releases, breaking news, taxes, trading costs and spread/liquidity checks are **not automated in this version**. An unusual move alert cannot explain why a company moved. A licensed intraday/news provider and additional analysis are required for comprehensive event detection and intraday alerts. Historical performance and these unbacktested rules cannot establish a best time to buy or sell.

The Yahoo Finance chart endpoint is unofficial, may be delayed, rate limited, unavailable or change without notice. It is a prototype data source, not an exchange-grade feed. Provider failures are explicit in reports and briefly noted in the daily summary. Benchmark failure disables new buy candidates. No trading decision should be inferred from missing data. Review Yahoo/provider terms for your intended use and replace the adapter if needed.

## Validate

```sh
python3 -m unittest discover -s tests -v
```

Tests cover partial/stale candles, RSI, breakdown exits, separate horizons, retryable delivery failures and persistent deduplication.

## Reference sources

- Telegram Bot API (`getMe`, `getUpdates`, `sendMessage`): https://core.telegram.org/bots/api
- Telegram bot creation: https://core.telegram.org/bots/features#botfather
- CIBC Micron CDR details: https://cdr.cibc.com/en/cdr-directory/MU
- Fidelity FINN mandate: https://www.fidelity.ca/en/products/etfs/finn/
- Sprott SPPP trust and listing currencies: https://sprott.com/investment-strategies/exchange-listed-products/physical-bullion-funds/platinum-and-palladium/

# Follow-ups after a snapshot

Use the `facts` returned by `company_snapshot` (symbol, price, period change, peer prices) for quick
answers; call a tool only when you need data that isn't there.

| User asks | Do this |
|---|---|
| "What's the price now?" | `get_stock_price` |
| "Show it over 1 year instead" | `company_snapshot` again with the new `period` |
| "How did it do between March and June?" | `get_stock_price_history` with `start_date` / `end_date` |
| "More news" / "anything older?" | `get_company_news` with a larger `days` (max 30) |
| "Which peer is up most today?" / "How is AMD doing today?" | the `peers` list in `facts` (price, today's %) |
| "Tell me about <peer>" | `company_snapshot` for that peer |
| "Compare it with <peer>" | `get_stock_price_history` for both, same `period`; give both % changes side by side |
| "What does it do?" / "Where is it listed?" | `get_company_profile` |
| "What's moving the market?" | `get_market_news` |

Keep comparison answers short: one line per company with the period % change, then one
sentence on the difference.

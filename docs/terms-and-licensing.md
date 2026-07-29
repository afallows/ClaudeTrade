# Terms of Service and Licensing

This document clarifies your obligations when using ClaudeTrade and third-party data sources.

## ClaudeTrade Project Licence

**ClaudeTrade is released under the MIT Licence.**

Summary:

- You can use, modify, and distribute the software
- The software is provided "as-is" without warranty
- You must include the original licence notice in distributions
- The authors are not liable for losses or damages

Full licence text is in the repository root.

---

## Third-Party Data Sources

### Market Data

#### Stooq (Free Tier)

**Provider**: Stooq (stooq.com)  
**Data**: Daily OHLCV bars  
**Access**: HTTP API  
**Licence**: Educational/research use only  
**Restrictions**:
- No redistribution or commercial use without a paid licence
- Free data is suitable only for personal research
- Stooq retains all rights to the data

**Your obligations**:
- Do not redistribute Stooq data
- Do not use for commercial purposes without a paid licence
- Do not exceed the rate limits (configured as 60 calls/minute)
- Respect the terms at https://stooq.com/

**Practical impact**: If you use Stooq data in a backtest and publish the results, you must disclose that it's Stooq data and not redistribute the raw OHLCV.

#### CSV Data (Provided by You)

**Provider**: Your own files  
**Licence**: You are responsible for the licence of any data you provide

When you supply a CSV file with market data:

1. You must own or have permission to use the data
2. You are responsible for its accuracy
3. You must comply with any terms of the original source

If you obtained the CSV from a paid data provider (e.g., Bloomberg, FactSet, Polygon), you must respect their terms.

#### Synthetic Data

**Provider**: Generated locally  
**Licence**: Generated on your machine; carries no third-party licence restrictions

Synthetic data is fabricated for testing and engine validation only. It does not represent real markets.

---

### Earnings Calendar Data

#### Synthetic Earnings

**Licence**: Generated locally; no restrictions

#### CSV Earnings (Provided by You)

**Licence**: You are responsible  
**Example sources**:
- Seeking Alpha (free, but terms of service may apply)
- Earnings Whispers (historical data available via third-party integrations)
- Your own research

Respect the licence and terms of your source.

---

### Social Media Data

#### Reddit

**Provider**: Reddit, Inc. (https://reddit.com)  
**API**: Official OAuth API  
**Access requirement**: Valid credentials (client ID, client secret)

**Terms of service** (from https://www.reddit.com/r/rules/):

1. You can fetch public posts and comments for non-commercial research
2. You must respect Reddit's API rate limits (currently 60 calls/minute for public OAuth)
3. You cannot scrape or download large volumes for redistribution
4. You cannot use data to train commercial models without permission
5. You must provide a descriptive User-Agent string (ClaudeTrade does this)

**Your obligations**:
- Do not redistribute Reddit data
- Do not use for commercial purposes (e.g., selling sentiment signals)
- Respect rate limits
- Acknowledge Reddit as the source if publishing results
- Respect individual post privacy (do not dox authors)

**How ClaudeTrade complies**:
- Fetches only public subreddit posts (no private communities)
- Stores only sanitised text and aggregates (no usernames, only hashes)
- Respects configured rate limits
- Does not attempt to bulk-download or scrape

---

#### X (Twitter)

**Provider**: X Corp. (https://x.com)  
**API**: X API v2 (paid tier required)  
**Access requirement**: Valid bearer token (requires paid account)

**Terms of service** (from https://x.com/en/developers):

1. Academic research and non-commercial use are permitted under specific licences
2. Commercial use requires a paid tier
3. You cannot redistribute raw tweet data
4. You must aggregate and summarise rather than republish
5. Rate limits vary by tier (Free: very limited; Standard: ~15 calls/min; Premium: ~300 calls/min)

**Your obligations**:
- Pay for a tier appropriate to your usage
- Do not redistribute tweet data
- Aggregate and report only summary statistics if publishing results
- Respect rate limits
- Acknowledge X as the source

**How ClaudeTrade complies**:
- Requires paid API tier (config enforces this)
- Stores only sanitised text and aggregates (no usernames)
- Respects configured rate limits
- Does not attempt to bulk-export tweet data

---

### AI Providers

#### OpenAI (ChatGPT, GPT-4)

**Provider**: OpenAI (https://openai.com)  
**API**: OpenAI API (paid, usage-based)  
**Access requirement**: API key + payment method

**Terms of service** (from https://openai.com/policies/api-data-usage):

1. You own outputs generated from your inputs
2. Data sent to OpenAI is not used to train models (by default)
3. You are charged per token (input + output)
4. You agree not to use for illegal purposes
5. OpenAI retains the right to suspend your account for abuse

**Your obligations**:
- Pay OpenAI for API usage
- Do not use for illegal or abusive purposes
- Secure your API key (treat as a password)
- Monitor costs (set daily/monthly limits)
- Respect OpenAI's usage policies

**How ClaudeTrade complies**:
- Stores credentials securely (env var or OS credential store)
- Tracks costs locally (config specifies `daily_cost_limit_usd`)
- Does not send raw usernames or personal data
- Sanitises input before sending (removes URLs, instructions, etc.)

---

#### Anthropic (Claude)

**Provider**: Anthropic (https://anthropic.com)  
**API**: Claude API (paid, usage-based)  
**Access requirement**: API key + payment method

**Terms of service** (from https://www.anthropic.com/legal):

1. You own outputs
2. API requests are not used to train Claude (by default)
3. You are charged per token
4. Anthropic handles sensitive data responsibly
5. Anthropic retains the right to suspend abuse

**Your obligations**:
- Pay Anthropic for API usage
- Secure your API key
- Monitor costs
- Respect usage policies

**How ClaudeTrade complies**:
- Stores credentials securely
- Tracks costs locally
- Sanitises social posts before sending
- Does not send personal information

---

### Market Data Restrictions by Geography

**US Equities Only**: ClaudeTrade is designed for US equity markets. International markets have different:

- Trading hours and holidays
- Regulatory frameworks
- Settlement rules
- Tax treatment

Extending to international markets would require:

1. Symbol universe expansion
2. Holiday calendar updates
3. Multi-currency support
4. Currency hedging models (optional)
5. Tax efficiency models per jurisdiction

**Current constraint**: Data providers (Stooq, Reddit, X) have limited non-US coverage.

---

## Your Responsibilities

### Regulatory Compliance

ClaudeTrade is a research tool, not a regulated financial service. **You are responsible for compliance in your jurisdiction.**

Examples:

1. **Is swing trading legal where you are?** 
   - US: Generally yes (but pattern-day trading rules apply to day traders)
   - UK: Yes (but FCA regulations may apply if trading on behalf of others)
   - China: Restricted or prohibited for retail traders
   - Varies by jurisdiction: Check your local laws

2. **Do you need a licence or registration?**
   - If trading for others, you likely need registration
   - If trading your own account for research, probably exempt
   - If offering signals as a service, definitely regulated

3. **Are there tax implications?**
   - Short-term capital gains are taxed higher than long-term in the US
   - Day-trading losses have wash-sale rules
   - International brokers may have different tax reporting

**You must ensure your use of ClaudeTrade complies with local law.**

### Data Privacy

When you use social media providers (Reddit, X):

1. **You are collecting public data** — but that doesn't mean you can do anything with it
2. **You are responsible for GDPR, CCPA, and other privacy laws** if you're in/targeting those regions
3. **Author hashes are pseudonymised** — but they are not cryptographically secure; do not assume anonymity

Example: If you publish a backtest and say "sentiment from r/stocks was positive", that's fine. If you say "this specific user said X on Reddit", that's a privacy violation.

### API Rate Limits

The system respects configured rate limits for:

- Stooq (60 calls/minute default)
- Reddit OAuth (60 calls/minute official guidance)
- X API v2 (15 calls/minute for standard, more for premium)

**Do not attempt to circumvent rate limits.** This can result in:

1. IP ban from the provider
2. Account suspension
3. Legal action (unlikely, but possible)

### Broker Terms

If you connect ClaudeTrade to a live broker (when live trading is implemented):

1. **Broker terms apply** to all trades
2. **Pattern-day trading rules** may limit your trading frequency
3. **Margin requirements** may prevent some trades
4. **Borrow availability** may prevent some short trades

The simulator does not model these; real-world execution will differ.

---

## Attribution and Citation

If you publish backtest results using ClaudeTrade:

### Recommended Attribution

**In your report**:

> Backtest conducted using ClaudeTrade (version X.X.X; https://github.com/afallows/claudetrade)
>
> Market data from [Stooq / CSV / Synthetic]  
> Social sentiment from [Reddit / X / Synthetic]  
> Earnings calendar from [CSV / Synthetic]  

### Disclosure of Data Limitations

**Always disclose**:

1. That results are simulated, not live
2. What data sources were used
3. Transaction costs assumed
4. Constraints or filters applied
5. Hold period assumptions
6. Rebalancing frequency (if any)

Example:

> Backtests assume 3 bps half-spread, commission-free execution, and 1–2% risk per trade. Live results will likely be worse. These are research signals, not investment advice.

---

## License Compliance Summary

| Data Source | Your Obligation | Restriction |
|-------------|-----------------|-------------|
| **ClaudeTrade code** | Attribution (MIT) | None; modify freely |
| **Stooq OHLCV** | Educational use only; no redistribution | Free tier no commercial use |
| **Your own CSV** | Comply with the source licence | Depends on original source |
| **Synthetic data** | None | No restrictions |
| **Reddit posts** | Aggregate/summarise only; no redistribution | Non-commercial; rate limits |
| **X tweets** | Aggregate only; paid API tier required | Non-commercial; rate limits |
| **OpenAI API** | Pay for usage; secure credentials | Non-illegal use |
| **Anthropic API** | Pay for usage; secure credentials | Non-illegal use |

---

## Disclaimer

**ClaudeTrade generates research signals, not investment advice. Use at your own risk.**

The authors make no warranties about:

- Accuracy of signals
- Performance in live markets
- Suitability for your investment goals
- Compliance with your local laws
- Third-party data accuracy

**Any losses are your responsibility.**

By using ClaudeTrade, you accept these terms and agree to comply with all applicable laws and third-party terms of service.

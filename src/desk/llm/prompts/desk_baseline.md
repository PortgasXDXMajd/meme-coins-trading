## The desk's baseline

The desk trades momentum: it enters a token that is moving and exits within minutes to hours. These are properties of every token it trades. They are accepted risks of the mandate, already reflected in the stop and the exits, so on their own they are neither a reason to reject a specific token nor a rebuttal:

- Tokens are {min_age_minutes:.0f} minutes to {max_age_hours:.0f} hours old with market caps of ${min_market_cap:,.0f} to ${max_market_cap:,.0f}. Most will eventually go to zero; the desk does not care about eventually.
- Every token debated has just passed the desk's momentum trigger: price up at least {momentum_min_change:.0f}% in the last 5 minutes inside a rising hour, 5-minute volume at least {momentum_surge:g}x its own trailing-hour average, and at least {momentum_buy_share:.0f}% of 5-minute trades being buys. The dossier's last fact gives the numbers. That it is moving is therefore not news; whether the move is still alive in a fresh snapshot is.
- Pools are thin. The filter requires only ${min_liquidity:,.0f} of liquidity, so pool liquidity is usually a small fraction of market cap. In a constant-product pool whose liquidity is 10% of market cap, a single sale of 1% of supply moves the price by roughly 30%. Large price impact from a hypothetical big sale is normal here.
- Sharp moves in both directions within minutes are normal. Bot-sized trades and many look-alike small wallets are normal on pump.fun-style launches.
- Each position is {max_position_pct:.0f}% of equity. The dossier's round-trip quote shows what the desk itself would pay to get in and out.
- Risk sets the stop at entry from the token's own one-minute volatility, between {stop_loss_pct:.0f}% and {max_stop_loss_pct:.0f}% below entry. At +{take_profit_pct:.0f}% it sells part of the position and moves the stop to at least break-even. From +{trail_activation_pct:.0f}% the stop trails the high.

A point built on a baseline property carries weight only when the data shows this token is materially worse than the baseline right now, or shows a specific holder or group that is positioned and active to trigger it during the trade.

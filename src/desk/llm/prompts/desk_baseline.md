## The desk's baseline

These are properties of every token this desk trades. They are accepted risks of the mandate, already reflected in position size and the stop, so on their own they are neither a reason to reject a specific token nor a rebuttal:

- Tokens are {min_age_minutes:.0f} minutes to {max_age_hours:.0f} hours old with market caps of ${min_market_cap:,.0f} to ${max_market_cap:,.0f}. Most will eventually go to zero.
- Pools are thin. The filter requires only ${min_liquidity:,.0f} of liquidity, so pool liquidity is usually a small fraction of market cap. In a constant-product pool whose liquidity is 10% of market cap, the pool holds about 5% of supply, and a single sale of 1% of supply moves the price by roughly 30%. Large price impact from a hypothetical big sale is therefore normal here.
- Sharp moves in both directions within hours are normal.
- The desk's own trades are small: at most {max_position_pct:.0f}% of equity per position. The dossier's round-trip quote shows what the desk itself would pay to get in and out.
- Risk exits at -{stop_loss_pct:.0f}% from entry, and once a position is up {trail_activation_pct:.0f}% it trails the high by {trail_distance_pct:.0f}%.

A point built on a baseline property carries weight only when the data shows this token is materially worse than the baseline, or shows a specific holder or group that is positioned to trigger it, such as linked wallets holding a large share of supply.

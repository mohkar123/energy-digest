> Example context — replace with your own product/company context.

# ExampleCo Forecast context

## What ExampleCo Forecast is
ExampleCo Forecast is a (fictional) load-forecasting platform for utilities and energy retailers. It turns meter, network, SCADA and weather data into operational short-term forecasts, planning profiles and scenario-based long-term demand forecasts, plus a set of market-facing products built on the same forecasts.

## Product lines and what moves them
- **STF (short-term forecast):** 15 minutes to 14 days ahead, delivered as P50/P90/P98 quantiles at meter, feeder, substation or system level. Moved by weather extremes, heat or cold events, rapid DER/solar/BESS uptake, load shape changes, and demand-response or tariff changes that alter intraday shape.
- **LTF (long-term forecast):** 14 days to 30 years, scenario-based. Base is organic growth only; the gap between Base and a scenario is the energy-transition contribution. Levers cover EV adoption, electrification (heat pumps, gas conversion), DER utilisation, and a step-change ledger of committed large loads (data centres, industrial, electrified transport). Moved by large-load and data-centre pipelines, EV and electrification policy, IRP and load-forecast filings, regulator planning methodology, and published national scenarios.
- **BTM Insights:** detects and disaggregates behind-the-meter EV charging and solar PV from smart-meter data. Moved by EV and rooftop-solar adoption data, smart-meter rollouts, managed-charging and DER programmes, and privacy/data-access rules.
- **Coincident-peak alerts:** warns when a system peak that sets transmission or capacity charges is likely (ERCOT 4CP, PJM 5CP). Moved by changes to 4CP/5CP rules and charge methodology, capacity prices, summer heat forecasts, and large-load behaviour at peak.
- **Load scheduling:** day-ahead load schedules submitted to ISOs such as PJM, ISO-NE, NYISO, MISO and ERCOT. Moved by market rule and timeline changes, day-ahead and real-time price structure, and settlement penalties for deviation.
- **Settlements:** ISO charge allocation and settlement estimation, including ERCOT RTC+B (real-time co-optimisation plus batteries). Moved by settlement reform, new charge types, ancillary service redesign, and BESS participation rules.

## Customers and prospects by market
Roles follow config/watchlist.yaml exactly; never promote a prospect to a customer. All names below are fictional.
- **US:**
  - **Customers:** Northwind Utility (Northwind), Harbor Power Co-op.
  - **Prospects:** Prairie Electric Cooperative.
  - **Competitors:** Acme Analytics, Fabrikam Forecasting.
  - Also ERCOT/PJM retailers.
- **UK:**
  - **Customers:** Highland Networks; MHHS (Market-wide Half-Hourly Settlement) is the key driver.
  - **Prospects:** Thames Valley Supply.
- **BR:**
  - **Customers:** Verde Energia; the BESS market opening, ONS scheduling and PLD (spot price) exposure.
  - **Prospects:** none.
- **NZ:**
  - **Customers:** Southern Cross Lines, a distributor with about 300k ICPs (connections); deployment covering STF, BTM insights and LTF.
  - **Prospects:** none.
- **Other:**
  - **Prospects:** Maple Grid Distribution (Ontario).
  - **Partners:** Contoso Grid, asset-investment planning that can consume forecasts.
  - **Competitors:** Globex Energy Systems.

## What moves Forecast (relevance heuristics)
Score an item high when it:
- shows large-load or data-centre growth (new interconnection requests, tariffs, moratoria, utility load-growth revisions); EV, heat-pump or electrification policy; DER, BESS and curtailment developments;
- changes ISO market rules (ERCOT RTC+B and 4CP; PJM 5CP and capacity prices; UK MHHS; Brazil PLD/ONS);
- reports weather extremes affecting demand or peaks; NZ Commerce Commission and asset-management-plan (AMP) cycles;
- involves IRP and load-forecast filings in rate cases;
- concerns settlement reform or competitor moves (other forecasting, load-disaggregation, or DER-analytics vendors winning or launching).

Score low: generic commodity price moves, pure generation M&A, retail marketing, and items with no demand-side, load-forecast, settlement or planning implication. Prefer items naming a specific customer, regulator docket, rule or date.

## Adjacent products
Rate analysis and rate design are adjacent products; rate cases link to Forecast through test-year sales volumes, weather-normalised sales and peak-demand forecasts that regulators scrutinise.

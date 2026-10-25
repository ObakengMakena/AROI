-- Starter keyword searches (agriculture pilot), built from the blueprint's search taxonomy.
-- These run IN ADDITION to the country+field sweep, to catch relevant research from
-- neighbouring countries that could be adapted to South Africa.
-- extra_filter restricts results to authors at institutions in Southern & East Africa.
-- Edit freely in the Supabase table editor; set active=false to pause a query.

insert into search_queries (sector, theme, query_text, extra_filter) values
  ('agriculture', 'Climate resilience', '(drought OR "heat stress") AND (tolerance OR "water-use efficiency")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Climate resilience', '"climate-smart agriculture" AND smallholder', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Soil and inputs', '(biofertilizer OR biofertiliser OR "soil amendment" OR biochar) AND yield', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Soil and inputs', '("biological control" OR biopesticide) AND (maize OR citrus OR vegetables)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Crop productivity', '("plant disease" OR "crop disease") AND (detection OR diagnostic) AND (smartphone OR "low-cost" OR "deep learning")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Food preservation', '("post-harvest loss" OR "shelf life" OR "shelf-life") AND (storage OR coating OR packaging)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Food preservation', '("grain storage" OR hermetic OR "evaporative cooling")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Agricultural waste', '("agro-industrial" OR "crop residue" OR pomace OR husk OR peel) AND (valorisation OR valorization OR "value-added")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Livestock', '("animal feed" OR "insect meal" OR "black soldier fly") AND (protein OR growth)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Livestock', '(livestock OR cattle OR poultry) AND ("point-of-care" OR "rapid test" OR diagnostic)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Food processing', '(fermentation OR "solar drying" OR "solar dryer" OR milling) AND (sorghum OR cassava OR "indigenous" OR legume)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('agriculture', 'Smallholder tools', '("soil moisture" OR irrigation) AND (sensor OR IoT OR "low-cost") AND (farm OR smallholder)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw')
on conflict (sector, query_text) do nothing;

-- Additional sectors. Note: no * wildcards — OpenAlex rejects them in normal (stemmed) search.
insert into search_queries (sector, theme, query_text, extra_filter) values
  ('energy', 'Storage', '("second-life" OR "second life" OR repurposed) AND (battery OR batteries)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Storage', '("energy storage" OR "battery") AND ("load shedding" OR "load-shedding" OR "power outage" OR backup)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Solar', '(photovoltaic OR "solar PV") AND (soiling OR dust OR cleaning OR "module degradation")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Mini-grids', '("mini-grid" OR minigrid OR microgrid) AND (rural OR "energy access" OR tariff)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Clean cooking', '("clean cooking" OR cookstove OR briquette OR biogas OR "biomass pellet")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Efficiency', '("solar water heater" OR "heat pump" OR "energy efficiency") AND (household OR building OR industrial)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('energy', 'Hydrogen', '("green hydrogen" OR electrolyser OR electrolyzer OR "power-to-x")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('ai', 'African languages', '("natural language processing" OR "speech recognition" OR "machine translation" OR "language model") AND (isiZulu OR isiXhosa OR Sesotho OR Setswana OR Afrikaans OR "African languages" OR Swahili)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('ai', 'Computer vision', '("computer vision" OR "deep learning" OR "image classification") AND (mining OR crop OR livestock OR infrastructure OR retail)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('ai', 'Edge AI', '(TinyML OR "edge computing" OR "on-device" OR "low-power") AND ("machine learning" OR "neural network")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('ai', 'Fraud and finance', '("fraud detection" OR "credit scoring" OR "mobile money") AND ("machine learning" OR "deep learning")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('ai', 'Forecasting', '("load forecasting" OR "demand forecasting" OR "yield prediction") AND ("machine learning" OR "deep learning")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Low-cost materials', '(geopolymer OR "fly ash" OR slag OR "alkali-activated") AND (concrete OR brick OR mortar)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Low-cost materials', '("compressed earth" OR "stabilised earth" OR "stabilized earth" OR "rammed earth" OR adobe)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Low-cost materials', '("recycled aggregate" OR "construction and demolition waste" OR "plastic waste") AND (concrete OR brick OR paver)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Housing', '("informal settlement" OR "low-cost housing" OR "affordable housing") AND (fire OR thermal OR upgrading OR "building material")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Efficient buildings', '("thermal performance" OR "thermal comfort" OR "passive design") AND (housing OR dwelling OR building)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Construction methods', '("3D printing" OR "3D printed" OR prefabricated OR modular) AND (concrete OR housing OR construction)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw'),
  ('construction', 'Proptech', '("property valuation" OR "house price" OR "real estate" OR "mass appraisal") AND ("machine learning" OR hedonic OR model)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Volatility and options', '(volatility AND (forecasting OR "implied volatility" OR GARCH)) AND (JSE OR "South Africa" OR "emerging market")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Volatility and options', '("option pricing" OR "options market" OR "implied volatility" OR "volatility surface")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Microstructure', '("market microstructure" OR "bid-ask spread" OR liquidity OR "order flow") AND (JSE OR "stock exchange")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Portfolio risk', '("value-at-risk" OR "expected shortfall" OR "tail risk" OR "portfolio optimisation" OR "portfolio optimization")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Return predictability', '(momentum OR "factor model" OR anomaly OR "return predictability") AND (JSE OR "emerging market" OR African)', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm'),
  ('quant_finance', 'Alternative data', '("alternative data" OR sentiment OR "news analytics" OR "machine learning") AND ("stock returns" OR "asset pricing")', 'institutions.country_code:za|bw|na|zw|zm|mz|mw|ls|sz|ke|tz|ug|et|rw|ng|gh|eg|ma|tn|sn|ci|cm')
on conflict (sector, query_text) do nothing;

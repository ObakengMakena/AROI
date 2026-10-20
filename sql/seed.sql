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

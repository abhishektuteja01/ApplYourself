import re
import unicodedata
from dataclasses import dataclass
import geonamescache


def _fold(s: str) -> str:
    """Lowercase and strip diacritics so scraped ASCII forms match the
    accented geonames spellings (e.g. "Iasi" -> geonames "Iaşi", "Zurich"
    -> "Zürich", "Sao Paulo" -> "São Paulo")."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    ).lower()

@dataclass
class LocationParse:
    country: str
    state: str
    city: str
    remote: bool

gc = geonamescache.GeonamesCache()
US_STATES = gc.get_us_states()
COUNTRIES = gc.get_countries()
CITIES = gc.get_cities()

# Build lookups
STATE_CODES = {v['code']: v['name'] for v in US_STATES.values()}
STATE_NAMES = {v['name'].lower(): v['code'] for v in US_STATES.values()}

COUNTRY_NAMES = {v['name'].lower(): v['name'] for v in COUNTRIES.values()}
COUNTRY_NAMES.update({
    'usa': 'United States',
    'us': 'United States',
    'united states': 'United States',
    'united states of america': 'United States',
    'uk': 'United Kingdom',
})

US_CITIES = {}
# Sort by population descending so we get the biggest city for a name
for city in sorted(CITIES.values(), key=lambda x: x.get('population', 0), reverse=True):
    if city['countrycode'] == 'US':
        name = city['name'].lower()
        if name not in US_CITIES:
            US_CITIES[name] = city

# ISO2 country code -> canonical country name, for turning a foreign city's
# `countrycode` into the same country string the country-name matcher emits.
CC_TO_COUNTRY = {code: data['name'] for code, data in COUNTRIES.items()}

# Diacritic-folded set of every US city name, so a foreign city that is ALSO
# a US city (Paris/Manchester/Cambridge/Vancouver/Portland ...) is left to the
# US matcher and never counts as a foreign signal — we stay conservative and
# read a bare namesake as its US city.
US_CITY_FOLDED = {_fold(name) for name in US_CITIES}

# Major non-US cities, keyed by diacritic-folded lowercase name -> country.
# Population floor keeps this to real world cities (and off short place-names
# that collide with ordinary tokens); namesake collisions with US cities are
# excluded above. Sorted by population so the biggest bearer of a name wins
# (e.g. "london" -> London, GB, not London, ON).
FOREIGN_CITY_MIN_POP = 150000
FOREIGN_CITIES: dict[str, str] = {}
for city in sorted(CITIES.values(), key=lambda x: x.get('population', 0), reverse=True):
    if city['countrycode'] == 'US':
        continue
    if city.get('population', 0) < FOREIGN_CITY_MIN_POP:
        continue
    folded = _fold(city['name'])
    if folded in US_CITY_FOLDED or folded in FOREIGN_CITIES:
        continue
    country = CC_TO_COUNTRY.get(city['countrycode'], "")
    if country:
        FOREIGN_CITIES[folded] = country

def parse_location(raw: str) -> LocationParse:
    if not raw:
        return LocationParse("", "", "", False)
        
    remote = "remote" in raw.lower()
    
    # We want to parse out locations.
    # To avoid matching "New York, London or Singapore" to just NY,
    # we can check if there are multiple parts separated by " or ", " and ", "&", or "/"
    # But let's first clean the string.
    
    # Let's extract all possible signals.
    found_countries = set()
    found_states = set()
    found_city_names = set()
    
    text = raw
    text_lower = text.lower()
    
    # 4. Country match
    # Look for countries in the string (word boundaries)
    for c_name, c_canon in COUNTRY_NAMES.items():
        if re.search(r'\b' + re.escape(c_name) + r'\b', text_lower):
            found_countries.add(c_canon)
            
    # 2. State match
    # full state names
    for s_name, s_code in STATE_NAMES.items():
        if re.search(r'\b' + re.escape(s_name) + r'\b', text_lower):
            found_states.add(s_code)
            
    # two-letter abbreviations ONLY when preceded by a comma+space token boundary ("Austin, TX")
    state_code_matches = re.findall(r',\s+([A-Z]{2})\b', text)
    for code in state_code_matches:
        if code in STATE_CODES:
            found_states.add(code)
            
    # 3. City match (US cities only)
    # This is tricky because city names can be generic words. 
    # We should probably only match cities if they are in the string as whole words.
    # To make it fast, we could split the string into tokens and n-grams and check.
    # Or just use the string text
    # Unicode letters (not just a-z) so accented city spellings like
    # "Zürich"/"São Paulo" tokenize as one word and fold to their ASCII form.
    words = re.findall(r'[^\W\d_]+', text, re.UNICODE)
    # Generate 1 to 3 word n-grams
    ngrams = []
    for i in range(len(words)):
        ngrams.append(words[i].lower())
        if i < len(words) - 1:
            ngrams.append(f"{words[i].lower()} {words[i+1].lower()}")
        if i < len(words) - 2:
            ngrams.append(f"{words[i].lower()} {words[i+1].lower()} {words[i+2].lower()}")
            
    for ngram in ngrams:
        if ngram in US_CITIES:
            # wait, if it's "New York", we find it.
            # but is it "San Francisco" in "San Francisco Bay Area"?
            found_city_names.add(ngram)
        foreign_country = FOREIGN_CITIES.get(_fold(ngram))
        if foreign_country:
            found_countries.add(foreign_country)

    # Now we need to reconcile.
    if found_states or found_city_names:
        found_countries.add("United States")

    # Multiple countries -> a multi-region listing. If the United States is
    # one of them it's a US-inclusive global role: don't guess a single spot,
    # return empty (the caller keeps it). If every country is foreign, the row
    # is positively non-US -> return a foreign country so the allowlist drops
    # it (rather than the old behavior of returning empty and keeping it).
    if len(found_countries) > 1:
        if "United States" in found_countries:
            return LocationParse("", "", "", remote)
        return LocationParse(sorted(found_countries)[0], "", "", remote)

    country = list(found_countries)[0] if found_countries else ""
    state = ""
    city = ""
    
    # If we found exactly one state, let's use it
    if len(found_states) == 1:
        state = list(found_states)[0]
        country = "United States"
    elif len(found_states) > 1:
        # multiple states? probably not a single location
        return LocationParse("", "", "", remote)
        
    # If we found multiple cities, we should be careful.
    # We can filter cities by the state if we found a state.
    valid_cities = []
    for c in found_city_names:
        city_data = US_CITIES[c]
        # if state is specified, city must match the state
        if state and city_data['admin1code'] != state:
            continue
        valid_cities.append(c)
        
    # What if no state was found, but we found a US city?
    # e.g., "San Francisco Bay Area" -> city "San Francisco", state CA, country US
    if not state and len(valid_cities) == 1:
        city_data = US_CITIES[valid_cities[0]]
        # A city hit fills state+country from geonames data when string didn't provide them.
        state = city_data['admin1code']
        country = "United States"
        city = city_data['name']
    elif len(valid_cities) == 1:
        city_data = US_CITIES[valid_cities[0]]
        city = city_data['name']
    return LocationParse(country, state, city, remote)

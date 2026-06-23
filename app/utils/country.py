"""Country-name normalization for player ``birth_country`` values.

Player bios arrive from several ingestion sources that disagree on encoding:
some store ISO-3166-1 alpha-2 codes (``US``, ``AU``), others full display
names (``United States``, ``Australia``), and a few use loose aliases
(``USA``, ``U.S.``).  This module collapses all of them onto a single
canonical display name so facets, filters, and stored data stay consistent.

Two entry points:

- :func:`canonical_country` — map any raw value to its canonical display name
  (used on write, in the facet list, and to clean existing data).
- :func:`country_variants` — given a canonical name, return every raw encoding
  that maps to it (used by the read-side filter so selecting one country still
  matches rows persisted under a code or alias).
"""

from __future__ import annotations

from typing import Optional

# ISO-3166-1 alpha-2 → canonical display name.  Names use the common short form
# (e.g. "United States", "South Korea") to match the full-name values already
# present in the data.
_ISO2_TO_NAME: dict[str, str] = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BO": "Bolivia",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CD": "DR Congo",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Ivory Coast",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cape Verde",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "French Guiana",
    "GH": "Ghana",
    "GM": "Gambia",
    "GN": "Guinea",
    "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GT": "Guatemala",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KH": "Cambodia",
    "KR": "South Korea",
    "KW": "Kuwait",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MG": "Madagascar",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NE": "Niger",
    "NG": "Nigeria",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "PA": "Panama",
    "PE": "Peru",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PR": "Puerto Rico",
    "PT": "Portugal",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "RW": "Rwanda",
    "SD": "Sudan",
    "SE": "Sweden",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "SV": "El Salvador",
    "SY": "Syria",
    "SZ": "Eswatini",
    "TD": "Chad",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TR": "Turkey",
    "TT": "Trinidad and Tobago",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "UK": "United Kingdom",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela",
    "VG": "British Virgin Islands",
    "VI": "U.S. Virgin Islands",
    "VN": "Vietnam",
    "YE": "Yemen",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}

# Loose full-name aliases (lower-cased) → canonical display name, for forms that
# are neither an ISO code nor already the canonical name.
_ALIASES: dict[str, str] = {
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "south korea": "South Korea",
    "korea": "South Korea",
    "republic of korea": "South Korea",
    "ivory coast": "Ivory Coast",
    "côte d'ivoire": "Ivory Coast",
    "cote d'ivoire": "Ivory Coast",
    "democratic republic of the congo": "DR Congo",
    "dr congo": "DR Congo",
    "czech republic": "Czechia",
    "bosnia": "Bosnia and Herzegovina",
}


def canonical_country(raw: Optional[str]) -> Optional[str]:
    """Return the canonical display name for a raw ``birth_country`` value.

    Args:
        raw: An ISO-2 code, a full country name, or a loose alias. ``None`` or
            blank input returns ``None``.

    Returns:
        The canonical display name. Unknown values are returned trimmed and
        otherwise unchanged so no data is silently dropped.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    # Two-letter alpha → ISO code lookup.
    if len(value) == 2 and value.isalpha():
        return _ISO2_TO_NAME.get(value.upper(), value.upper())

    alias = _ALIASES.get(value.lower())
    if alias is not None:
        return alias

    # Already a full name (canonical or an unmapped country): keep as-is.
    return value


def country_variants(canonical: str) -> set[str]:
    """Return every raw encoding that normalizes to ``canonical``.

    Used by the read-side filter so selecting "United States" matches rows still
    stored as ``US``, ``USA``, or ``U.S.``.

    Args:
        canonical: A canonical display name (typically a facet value).

    Returns:
        The set of stored forms that map to it, always including ``canonical``.
    """
    variants = {canonical}
    for code, name in _ISO2_TO_NAME.items():
        if name == canonical:
            variants.add(code)
    for alias, name in _ALIASES.items():
        if name == canonical:
            variants.add(alias)
            variants.add(alias.upper())
    return variants

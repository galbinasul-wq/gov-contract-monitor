"""AI-infrastructure-exposed publicly traded US companies for SEC 8-K monitoring.

These companies disclose material contract wins via SEC 8-K filings, especially
Item 1.01 (Entry into a Material Definitive Agreement). When a hyperscaler
(Microsoft, AWS, Google, Meta, Oracle, etc.) signs a major order, this is
where it shows up first -- often before the press picks it up.

Each entry has:
  ticker: stock symbol
  name:   display name
  cik:    SEC Central Index Key (optional; auto-resolved from ticker at runtime)
"""

WATCHLIST = [
    # ============ POWER / ELECTRICAL (data center infrastructure) ============
    {"ticker": "VRT",  "name": "Vertiv Holdings"},
    {"ticker": "ETN",  "name": "Eaton Corporation"},
    {"ticker": "GEV",  "name": "GE Vernova"},
    {"ticker": "HUBB", "name": "Hubbell"},
    {"ticker": "POWL", "name": "Powell Industries"},
    {"ticker": "ATKR", "name": "Atkore"},
    {"ticker": "AYI",  "name": "Acuity Brands"},
    {"ticker": "LFUS", "name": "Littelfuse"},

    # ============ COOLING / HVAC ============
    {"ticker": "MOD",  "name": "Modine Manufacturing"},
    {"ticker": "AAON", "name": "AAON Inc"},
    {"ticker": "LII",  "name": "Lennox International"},
    {"ticker": "TT",   "name": "Trane Technologies"},
    {"ticker": "CARR", "name": "Carrier Global"},

    # ============ NETWORKING EQUIPMENT ============
    {"ticker": "ANET", "name": "Arista Networks"},
    {"ticker": "CIEN", "name": "Ciena"},
    {"ticker": "CSCO", "name": "Cisco Systems"},
    {"ticker": "EXTR", "name": "Extreme Networks"},

    # ============ SERVERS / STORAGE ============
    {"ticker": "SMCI", "name": "Super Micro Computer"},
    {"ticker": "DELL", "name": "Dell Technologies"},
    {"ticker": "HPE",  "name": "Hewlett Packard Enterprise"},
    {"ticker": "NTAP", "name": "NetApp"},
    {"ticker": "PSTG", "name": "Pure Storage"},

    # ============ AI CHIPS / SEMICONDUCTORS ============
    {"ticker": "NVDA", "name": "Nvidia"},
    {"ticker": "AMD",  "name": "AMD"},
    {"ticker": "AVGO", "name": "Broadcom"},
    {"ticker": "MRVL", "name": "Marvell Technology"},
    {"ticker": "ALAB", "name": "Astera Labs"},
    {"ticker": "INTC", "name": "Intel"},

    # ============ SEMI EQUIPMENT ============
    {"ticker": "AMAT", "name": "Applied Materials"},
    {"ticker": "LRCX", "name": "Lam Research"},
    {"ticker": "KLAC", "name": "KLA Corporation"},

    # ============ MEMORY / STORAGE CHIPS ============
    {"ticker": "MU",   "name": "Micron Technology"},
    {"ticker": "WDC",  "name": "Western Digital"},
    {"ticker": "STX",  "name": "Seagate Technology"},

    # ============ OPTICS / PHOTONICS ============
    {"ticker": "COHR", "name": "Coherent Corp"},
    {"ticker": "LITE", "name": "Lumentum Holdings"},
    {"ticker": "AAOI", "name": "Applied Optoelectronics"},
    {"ticker": "FN",   "name": "Fabrinet"},

    # ============ CONTRACT MANUFACTURING / EMS ============
    {"ticker": "FLEX", "name": "Flex Ltd"},
    {"ticker": "JBL",  "name": "Jabil"},
    {"ticker": "CLS",  "name": "Celestica"},
    {"ticker": "SANM", "name": "Sanmina"},

    # ============ DATA CENTER CONSTRUCTION / EPC ============
    {"ticker": "PWR",  "name": "Quanta Services"},
    {"ticker": "MTZ",  "name": "MasTec"},
    {"ticker": "EME",  "name": "EMCOR Group"},
    {"ticker": "DY",   "name": "Dycom Industries"},
    {"ticker": "STRL", "name": "Sterling Infrastructure"},
    {"ticker": "IESC", "name": "IES Holdings"},

    # ============ POWER GENERATION (hyperscaler PPAs) ============
    {"ticker": "VST",  "name": "Vistra Corp"},
    {"ticker": "CEG",  "name": "Constellation Energy"},
    {"ticker": "TLN",  "name": "Talen Energy"},
    {"ticker": "NRG",  "name": "NRG Energy"},
]

"""Watchlist for the Form 4 insider-cluster monitor.

Insider open-market buying is the strongest signal in small/mid-caps, where
officers/directors genuinely have an information edge and a cluster of them
buying with their own money is meaningful. Mega-caps are included too -- an
open-market buy at that scale is rare and notable when it happens.

Each entry: ticker + name. CIK is auto-resolved from SEC's ticker map
(same mechanism as the SEC 8-K bot); add "cik" only if a ticker won't resolve.

Expand this freely. A natural extension is to mirror your gov-contract
watchlist so you can spot the confluence of "winning federal contracts" +
"insiders buying" on the same name.
"""

WATCHLIST = [
    # --- Defense / gov services (overlap with your contract bot) ---
    {"ticker": "KTOS", "name": "Kratos Defense"},
    {"ticker": "AVAV", "name": "AeroVironment"},
    {"ticker": "MRCY", "name": "Mercury Systems"},
    {"ticker": "VVX",  "name": "V2X Inc"},
    {"ticker": "ICFI", "name": "ICF International"},
    {"ticker": "DCO",  "name": "Ducommun"},
    {"ticker": "ATRO", "name": "Astronics"},
    {"ticker": "VSEC", "name": "VSE Corporation"},
    {"ticker": "CDRE", "name": "Cadre Holdings"},
    {"ticker": "DLHC", "name": "DLH Holdings"},

    # --- AI infrastructure (overlap with your SEC bot) ---
    {"ticker": "VRT",  "name": "Vertiv Holdings"},
    {"ticker": "POWL", "name": "Powell Industries"},
    {"ticker": "MOD",  "name": "Modine Manufacturing"},
    {"ticker": "AAON", "name": "AAON Inc"},
    {"ticker": "ALAB", "name": "Astera Labs"},
    {"ticker": "AAOI", "name": "Applied Optoelectronics"},
    {"ticker": "CLS",  "name": "Celestica"},
    {"ticker": "SANM", "name": "Sanmina"},
    {"ticker": "IESC", "name": "IES Holdings"},

    # --- Small/mid-cap industrials & energy ---
    {"ticker": "MTRX", "name": "Matrix Service"},
    {"ticker": "GLDD", "name": "Great Lakes Dredge & Dock"},
    {"ticker": "OII",  "name": "Oceaneering International"},
    {"ticker": "CECE", "name": "CECO Environmental"},
    {"ticker": "NPK",  "name": "National Presto Industries"},
    {"ticker": "LEU",  "name": "Centrus Energy"},
    {"ticker": "GEV",  "name": "GE Vernova"},
    {"ticker": "BWXT", "name": "BWX Technologies"},

    # --- Biotech / healthcare (insider buys are high-signal here) ---
    {"ticker": "EBS",  "name": "Emergent BioSolutions"},
    {"ticker": "NVAX", "name": "Novavax"},
    {"ticker": "EVH",  "name": "Evolent Health"},
    {"ticker": "HCSG", "name": "Healthcare Services Group"},

    # --- Financials / specialty ---
    {"ticker": "PLUS", "name": "ePlus Inc"},
    {"ticker": "NVEE", "name": "NV5 Global"},
    {"ticker": "DGII", "name": "Digi International"},
    {"ticker": "MITK", "name": "Mitek Systems"},
    {"ticker": "RPD",  "name": "Rapid7"},
    {"ticker": "TENB", "name": "Tenable Holdings"},
    {"ticker": "VRNS", "name": "Varonis Systems"},
    {"ticker": "ASGN", "name": "ASGN Incorporated"},

    # --- Larger names (rare but notable open-market buys) ---
    {"ticker": "PLTR", "name": "Palantir Technologies"},
    {"ticker": "SMCI", "name": "Super Micro Computer"},
    {"ticker": "CACI", "name": "CACI International"},
    {"ticker": "SAIC", "name": "Science Applications Intl"},
    {"ticker": "MMS",  "name": "Maximus"},
    {"ticker": "HII",  "name": "Huntington Ingalls"},

    # --- Added: law enforcement / drones / quantum ---
    # Insider open-market buying in speculative small-caps is especially
    # high-signal -- there's only one reason an insider buys their own
    # money-losing quantum stock on the open market.
    {"ticker": "AXON", "name": "Axon Enterprise"},
    {"ticker": "RCAT", "name": "Red Cat Holdings"},
    {"ticker": "QBTS", "name": "D-Wave Quantum"},
    {"ticker": "IONQ", "name": "IonQ"},
    {"ticker": "QUBT", "name": "Quantum Computing Inc"},
    {"ticker": "RGTI", "name": "Rigetti Computing"},
    {"ticker": "QCOM", "name": "Qualcomm"},
]

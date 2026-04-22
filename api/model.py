import json
import numpy_financial as npf
from http.server import BaseHTTPRequestHandler

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        result = run_model(body)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

def run_model(a):
    life        = int(a.get('life', 35))
    mwdc        = float(a.get('mwdc', 82))
    yield_kwh   = float(a.get('yield_kwh', 1710))
    avail       = float(a.get('avail', 0.985))
    degrad      = float(a.get('degrad', 0.004))
    curtail     = float(a.get('curtail', 0.02))
    ppa_price   = float(a.get('ppa_price', 59.0))
    ppa_esc     = float(a.get('ppa_esc', 0.0))
    ppa_tenor   = int(a.get('ppa_tenor', 5))
    merch_price = float(a.get('merch_price', 65.0))
    merch_esc   = float(a.get('merch_esc', 0.015))
    capex       = float(a.get('capex', 110432))
    loan        = float(a.get('loan', 27500))
    rate        = float(a.get('rate', 0.085))
    tenor       = int(a.get('tenor', 20))
    itc         = float(a.get('itc', 0.40))
    fmv         = float(a.get('fmv', 142545))
    discount    = float(a.get('discount', 0.08))
    opex_base   = float(a.get('opex_base', 1933))
    opex_esc    = float(a.get('opex_esc', 0.02))

    itc_proceeds = fmv * itc
    equity_in    = max(capex - loan - itc_proceeds, 1000)

    years = []
    cfs   = [-equity_in]

    for i in range(life):
        yr             = i + 1
        degrad_factor  = (1 - degrad) ** i
        gross_mwh      = mwdc * yield_kwh * avail * degrad_factor
        net_mwh        = gross_mwh * (1 - curtail)

        if yr <= ppa_tenor:
            price = ppa_price * ((1 + ppa_esc) ** i)
        else:
            price = merch_price * ((1 + merch_esc) ** (i - ppa_tenor))

        rev    = net_mwh * price / 1000
        opex   = opex_base * ((1 + opex_esc) ** i)
        ebitda = rev - opex

        beg_bal  = max(loan - (loan / tenor) * i, 0) if yr <= tenor else 0
        amort    = loan / tenor if yr <= tenor else 0
        interest = beg_bal * rate
        ds       = amort + interest if yr <= tenor else 0
        dscr     = ebitda / ds if ds > 0 else 99.0
        cfads    = max(ebitda - ds, 0)

        years.append({
            'year':         yr,
            'net_mwh':      round(net_mwh),
            'price':        round(price, 2),
            'revenue':      round(rev),
            'opex':         round(opex),
            'ebitda':       round(ebitda),
            'ebitda_margin':round(ebitda / rev * 100, 1) if rev > 0 else 0,
            'beg_bal':      round(beg_bal),
            'amort':        round(amort),
            'interest':     round(interest),
            'ds':           round(ds),
            'dscr':         round(dscr, 2),
            'cfads':        round(cfads),
        })
        cfs.append(cfads)

    try:
        irr = npf.irr(cfs)
        irr = round(float(irr) * 100, 2) if irr and not (irr != irr) else None
    except Exception:
        irr = None

    npv_val  = round(float(npf.npv(discount, cfs[1:])) - equity_in)
    min_dscr = min((y['dscr'] for y in years if y['ds'] > 0), default=0)
    payback  = next((y['year'] for y in years
                     if sum(c for c in cfs[1:y['year']+1]) > equity_in), None)

    flags = run_flags(a, irr, min_dscr)

    return {
        'irr':          irr,
        'npv':          npv_val,
        'min_dscr':     round(min_dscr, 2),
        'payback':      payback,
        'equity_in':    round(equity_in),
        'itc_proceeds': round(itc_proceeds),
        'years':        years,
        'flags':        flags,
    }

def run_flags(a, irr, min_dscr):
    flags = []
    dscr_cov   = float(a.get('dscr_covenant', 1.25))
    soiling    = float(a.get('soiling_loss', 0.015))
    degrad     = float(a.get('degrad', 0.004))
    merch      = float(a.get('merch_price', 65.0))
    itc        = float(a.get('itc', 0.40))
    safe_harbor= bool(a.get('safe_harbor', True))
    cont_pct   = float(a.get('contingency_pct', 0.035))
    capex      = float(a.get('capex', 110432))
    hurdle     = float(a.get('hurdle_rate', 10.0))

    if min_dscr < dscr_cov:
        flags.append({'severity':'critical','area':'Debt Service',
            'title':'DSCR Below Covenant',
            'desc':f'Min DSCR {min_dscr:.2f}x is below the {dscr_cov}x covenant.',
            'action':'Resize debt or renegotiate covenant before close.'})
    elif min_dscr < 1.40:
        flags.append({'severity':'major','area':'Debt Service',
            'title':'DSCR Headroom Thin',
            'desc':f'Min DSCR {min_dscr:.2f}x leaves limited headroom for underperformance.',
            'action':'Stress test with P90 yield and +15% OPEX scenario.'})

    if soiling < 0.02:
        flags.append({'severity':'major','area':'PVsyst / Energy',
            'title':'Soiling Loss Below Benchmark',
            'desc':f'{soiling*100:.1f}% soiling applied. Regional benchmark is 2.0–2.5%.',
            'action':'Request soiling study or apply conservative haircut.'})

    if degrad < 0.003:
        flags.append({'severity':'major','area':'PVsyst / Energy',
            'title':'Degradation Rate Optimistic',
            'desc':f'{degrad*100:.2f}%/yr is below the 0.40–0.50%/yr industry standard.',
            'action':'Confirm with module manufacturer warranty.'})

    if itc >= 0.40 and not safe_harbor:
        flags.append({'severity':'critical','area':'ITC / Tax',
            'title':'40% ITC Without Safe Harbor',
            'desc':'Safe Harbor not confirmed. ITC recapture risk.',
            'action':'Confirm Safe Harbor compliance before close.'})

    if merch > 70:
        flags.append({'severity':'major','area':'Revenue',
            'title':'Merchant Price Above Consensus',
            'desc':f'${merch}/MWh exceeds Aurora consensus of $65–70/MWh.',
            'action':'Align with Aurora base case or flag as bull case.'})

    if cont_pct < 0.05:
        flags.append({'severity':'minor','area':'CAPEX',
            'title':'Contingency Below 5%',
            'desc':f'{cont_pct*100:.1f}% contingency is below the recommended 5% minimum.',
            'action':'Increase contingency or confirm lump-sum EPC contract.'})

    if irr and irr < hurdle:
        flags.append({'severity':'critical','area':'Returns',
            'title':'IRR Below Hurdle Rate',
            'desc':f'Levered IRR {irr:.1f}% is below the {hurdle}% hurdle rate.',
            'action':'Reduce bid price or renegotiate terms.'})

    return flags
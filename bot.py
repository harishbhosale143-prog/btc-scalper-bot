import asyncio
import aiohttp
import json
import os
import time
import datetime
from collections import deque
from aiohttp import web

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get('TG_TOKEN', '8744510180:AAES8YSwWBxX_D6Lci46wCH3mi-1vjgupyo')
CHAT_ID = os.environ.get('TG_CHAT', '7359160966')
FETCH_SEC = 15
MIN_CONF = 7
PAPER_CAP = 10000
SYMBOL = 'BTCUSDT'

state = {
    'prices_5m': deque(maxlen=100),
    'prices_15m': deque(maxlen=60),
    'volumes_5m': deque(maxlen=25),
    'paper_bal': 10000.0,
    'paper_start': 10000.0,
    'trades': [],
    'active': None,
    'last_sig': None,
    'scan_count': 0,
    'D': {}
}

# ═══════════════════════════════════════════════════
#  WEB SERVER — UptimeRobot ping করার জন্য
# ═══════════════════════════════════════════════════
async def handle_ping(request):
    D = state['D']
    wins = sum(1 for t in state['trades'] if t['outcome'] == 'WIN')
    total = len(state['trades'])
    wr = round(wins/total*100) if total > 0 else 0
    return web.json_response({
        'status': 'alive',
        'btc': D.get('price', 0),
        'bull': D.get('bull_conf', 0),
        'bear': D.get('bear_conf', 0),
        'balance': state['paper_bal'],
        'trades': total,
        'win_rate': wr,
        'scan': state['scan_count'],
        'active': bool(state['active'])
    })

async def start_web():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/ping', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    print("Web server running on port 8080")

# ═══════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════
async def tg_send(session, text):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    try:
        await session.post(url, json={
            'chat_id': CHAT_ID,
            'text': text,
            'parse_mode': 'HTML'
        })
        print(f"TG: {text[:60]}")
    except Exception as e:
        print(f"TG error: {e}")

# ═══════════════════════════════════════════════════
#  BINANCE FETCH
# ═══════════════════════════════════════════════════
async def fetch_data(session):
    try:
        base = 'https://api.binance.us/api/v3'
        async with session.get(f'{base}/klines?symbol={SYMBOL}&interval=5m&limit=100') as r:
            k5 = await r.json()
        async with session.get(f'{base}/klines?symbol={SYMBOL}&interval=15m&limit=60') as r:
            k15 = await r.json()
        async with session.get(f'{base}/ticker/24hr?symbol={SYMBOL}') as r:
            tk = await r.json()
        async with session.get(f'{base}/depth?symbol={SYMBOL}&limit=20') as r:
            ob = await r.json()

        D = state['D']
        D['price'] = float(tk['lastPrice'])
        D['chg24'] = float(tk['priceChangePercent'])
        D['high24'] = float(tk['highPrice'])
        D['low24'] = float(tk['lowPrice'])
        D['open'] = float(k5[-1][1])

        p5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]
        p15 = [float(k[4]) for k in k15]

        state['prices_5m'] = deque(p5, maxlen=100)
        state['prices_15m'] = deque(p15, maxlen=60)
        state['volumes_5m'] = deque(v5, maxlen=25)

        bids = ob.get('bids', [])
        asks = ob.get('asks', [])
        bv = sum(float(b[1]) for b in bids[:10])
        av = sum(float(a[1]) for a in asks[:10])
        D['bid'] = float(bids[0][0]) if bids else D['price']
        D['ask'] = float(asks[0][0]) if asks else D['price']
        D['spread_pct'] = (D['ask'] - D['bid']) / D['bid'] * 100
        D['ba_ratio'] = bv / av if av > 0 else 1.0
        return True
    except Exception as e:
        print(f"Fetch error: {e}")
        return False

# ═══════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════
def ema(arr, n):
    if len(arr) < n: return arr[-1] if arr else 0
    k = 2/(n+1); e = arr[0]
    for v in arr[1:]: e = v*k + e*(1-k)
    return e

def calc_rsi(prices, period=14):
    if len(prices) < period+1: return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i]-prices[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:])/period
    al = sum(losses[-period:])/period
    if al == 0: return 100
    return round(100-100/(1+ag/al), 1)

def calc_indicators():
    D = state['D']
    p5 = list(state['prices_5m'])
    p15 = list(state['prices_15m'])
    v5 = list(state['volumes_5m'])
    if len(p5) < 26: return False

    D['ema9'] = ema(p5[-30:], 9)
    D['ema21'] = ema(p5[-40:], 21)
    prev9 = ema(p5[-31:-1], 9) if len(p5)>31 else D['ema9']
    prev21 = ema(p5[-41:-1], 21) if len(p5)>41 else D['ema21']
    D['ema_bull'] = D['ema9'] > D['ema21']
    D['ema_cross'] = (D['ema9']>D['ema21'] and prev9<=prev21) or (D['ema9']<D['ema21'] and prev9>=prev21)

    D['rsi5'] = calc_rsi(p5[-20:])

    e12 = ema(p5[-30:], 12); e26 = ema(p5[-40:], 26)
    D['macd'] = e12-e26; D['macd_bull'] = D['macd'] > 0
    prev_macd = ema(p5[-31:-1],12)-ema(p5[-41:-1],26) if len(p5)>41 else D['macd']
    D['macd_hist'] = D['macd']-prev_macd

    bb20 = p5[-20:]
    bm = sum(bb20)/len(bb20)
    bs = (sum((v-bm)**2 for v in bb20)/len(bb20))**0.5
    D['bb_upper'] = bm+2*bs; D['bb_lower'] = bm-2*bs; D['bb_mid'] = bm
    p = D['price']
    if p <= D['bb_lower']*1.001: D['bb_pos'] = 'lower'
    elif p >= D['bb_upper']*0.999: D['bb_pos'] = 'upper'
    elif p > bm: D['bb_pos'] = 'above_mid'
    else: D['bb_pos'] = 'below_mid'

    vm = sum(p5[-20:])/20
    vs = (sum((v-vm)**2 for v in p5[-20:])/20)**0.5
    D['volty'] = round(vs/vm*100, 3) if vm > 0 else 0

    if len(v5) >= 20:
        avg = sum(v5[-21:-1])/20
        D['vol_spike'] = v5[-1]/avg if avg > 0 else 1
    else:
        D['vol_spike'] = 1

    D['sup'] = min(p5[-20:]); D['res'] = max(p5[-20:])
    D['near_sup'] = (p-D['sup'])/p*100 < 0.3
    D['near_res'] = (D['res']-p)/p*100 < 0.3

    body = abs(p-D['open'])/D['open']*100
    D['candle_bull'] = p > D['open']; D['candle_size'] = body

    if len(p15) >= 21:
        D['htf_bull'] = ema(p15[-20:],9) > ema(p15[-30:],21)
        D['htf_rsi'] = calc_rsi(p15[-20:])
    else:
        D['htf_bull'] = True; D['htf_rsi'] = 50
    return True

# ═══════════════════════════════════════════════════
#  CONFLUENCE
# ═══════════════════════════════════════════════════
def calc_confluence():
    D = state['D']; bull = 0; bear = 0
    w = 3 if D.get('ema_cross') else 2
    if D.get('ema_bull'): bull += w
    else: bear += w

    rsi = D.get('rsi5', 50)
    if 35<=rsi<=48: bull+=2
    elif 52<=rsi<=65: bear+=2
    elif rsi<35: bull+=1
    elif rsi>65: bear+=1

    w = 2 if (D.get('macd_hist',0)>0 and D.get('macd_bull')) or \
              (D.get('macd_hist',0)<0 and not D.get('macd_bull')) else 1
    if D.get('macd_bull'): bull+=w
    else: bear+=w

    bp = D.get('bb_pos','mid')
    if bp=='lower': bull+=2
    elif bp=='upper': bear+=2
    elif bp=='above_mid': bull+=1
    else: bear+=1

    vs = D.get('vol_spike',1)
    if vs >= 1.8:
        if D.get('candle_bull'): bull+=2
        else: bear+=2

    ba = D.get('ba_ratio',1)
    if ba>1.3: bull+=1
    elif ba<0.7: bear+=1

    if D.get('htf_bull'): bull+=2
    else: bear+=2

    if D.get('candle_size',0)>0.08:
        if D.get('candle_bull'): bull+=1
        else: bear+=1

    D['bull_conf']=bull; D['bear_conf']=bear
    return bull, bear

# ═══════════════════════════════════════════════════
#  POSITION SIZING
# ═══════════════════════════════════════════════════
def calc_pos():
    vol = state['D'].get('volty',1)
    spike = state['D'].get('vol_spike',1)
    diff = abs(state['D'].get('bull_conf',0)-state['D'].get('bear_conf',0))

    if vol<0.1 or spike<0.15:
        return {'lev':0,'tp':0,'sl':0,'label':'BLOCKED','can_trade':False}
    elif vol<0.9: lev,tp,sl,label = 5,0.008,0.004,'SMALL'
    elif vol<2.0: lev,tp,sl,label = 10,0.010,0.005,'FULL'
    elif vol<4.0: lev,tp,sl,label = 5,0.014,0.007,'HALF'
    else: lev,tp,sl,label = 3,0.014,0.007,'MIN'

    if diff>=10 and 0.9<=vol<2.5:
        lev=min(lev+2,12); label+='+BONUS'

    return {'lev':lev,'tp':tp,'sl':sl,'label':label,'can_trade':True,'pos_size':PAPER_CAP*lev}

# ═══════════════════════════════════════════════════
#  EXECUTE TRADE
# ═══════════════════════════════════════════════════
async def execute_trade(session, sig_type, conf, reason, pos):
    D = state['D']
    entry = D['price']
    is_buy = sig_type=='BUY'
    target = entry*(1+pos['tp']) if is_buy else entry*(1-pos['tp'])
    sl = entry*(1-pos['sl']) if is_buy else entry*(1+pos['sl'])

    state['active'] = {
        'type':sig_type,'entry':entry,'target':target,'sl':sl,
        'trail_sl':sl,'peak':entry,'conf':conf,'lev':pos['lev'],
        'pos_size':pos['pos_size'],'label':pos['label'],
        'tp_pct':pos['tp'],'sl_pct':pos['sl'],
        'time':time.time(),'stage':0,'reason':reason
    }
    state['last_sig'] = sig_type

    emoji = '📈' if is_buy else '📉'
    msg = (
        f"{emoji} <b>AUTO {sig_type} EXECUTED!</b>\n\n"
        f"📍 Entry: <b>${entry:,.0f}</b>\n"
        f"🎯 Target: ${target:,.0f} (+{pos['tp']*100:.2f}%)\n"
        f"🛑 Stop Loss: ${sl:,.0f} (-{pos['sl']*100:.2f}%)\n"
        f"⚖ R:R = 1:{pos['tp']/pos['sl']:.1f}\n\n"
        f"📊 {pos['label']} · {pos['lev']}x · ₹{int(pos['pos_size']/1000)}K\n"
        f"⚡ Confluence: Bull{D['bull_conf']} Bear{D['bear_conf']} → {conf}%\n"
        f"🌐 HTF: {'BULL' if D['htf_bull'] else 'BEAR'} · RSI: {D['rsi5']}\n\n"
        f"💰 Balance: ₹{state['paper_bal']:,.0f}"
    )
    await tg_send(session, msg)

# ═══════════════════════════════════════════════════
#  SMART TRADE MANAGEMENT
# ═══════════════════════════════════════════════════
async def update_active(session):
    a = state['active']
    if not a: return
    D = state['D']
    cur = D['price']
    entry = a['entry']
    is_buy = a['type']=='BUY'

    profit_pct = (cur-entry)/entry*100 if is_buy else (entry-cur)/entry*100
    peak_profit = (a['peak']-entry)/entry*100 if is_buy else (entry-a['peak'])/entry*100

    if is_buy and cur>a['peak']: a['peak']=cur
    elif not is_buy and cur<a['peak']: a['peak']=cur

    # Multi-stage trailing SL
    if is_buy:
        if peak_profit>=0.8:
            new_sl=a['peak']*(1-0.0008)
            if new_sl>a['trail_sl']:
                a['trail_sl']=new_sl
                if a['stage']<4:
                    a['stage']=4
                    await tg_send(session,f"🎯 Trail SL Stage 4!\nBUY peak: +{peak_profit:.2f}%\nSL: ${a['trail_sl']:,.0f}")
        elif peak_profit>=0.6:
            new_sl=a['peak']*(1-0.0012)
            if new_sl>a['trail_sl']: a['trail_sl']=new_sl; a['stage']=max(a['stage'],3)
        elif peak_profit>=0.4:
            new_sl=a['peak']*(1-0.0015)
            if new_sl>a['trail_sl']: a['trail_sl']=new_sl; a['stage']=max(a['stage'],2)
        elif peak_profit>=0.15:
            new_sl=entry*1.0001
            if new_sl>a['trail_sl']:
                a['trail_sl']=new_sl
                if a['stage']<1:
                    a['stage']=1
                    await tg_send(session,f"🔒 BREAKEVEN SL!\nBUY trade secure झाला!\nPeak: +{peak_profit:.2f}%\nZero loss guaranteed!")
    else:
        if peak_profit>=0.8:
            new_sl=a['peak']*(1+0.0008)
            if new_sl<a['trail_sl']:
                a['trail_sl']=new_sl
                if a['stage']<4:
                    a['stage']=4
                    await tg_send(session,f"🎯 Trail SL Stage 4!\nSELL peak: +{peak_profit:.2f}%\nSL: ${a['trail_sl']:,.0f}")
        elif peak_profit>=0.6:
            new_sl=a['peak']*(1+0.0012)
            if new_sl<a['trail_sl']: a['trail_sl']=new_sl; a['stage']=max(a['stage'],3)
        elif peak_profit>=0.4:
            new_sl=a['peak']*(1+0.0015)
            if new_sl<a['trail_sl']: a['trail_sl']=new_sl; a['stage']=max(a['stage'],2)
        elif peak_profit>=0.15:
            new_sl=entry*0.9999
            if new_sl<a['trail_sl']:
                a['trail_sl']=new_sl
                if a['stage']<1:
                    a['stage']=1
                    await tg_send(session,f"🔒 BREAKEVEN SL!\nSELL trade secure झाला!\nZero loss guaranteed!")

    active_sl = a['trail_sl']
    tp_hit = (is_buy and cur>=a['target']) or (not is_buy and cur<=a['target'])
    sl_hit = (is_buy and cur<=active_sl) or (not is_buy and cur>=active_sl)

    if tp_hit:
        await close_trade(session,cur,'TP_HIT','Full target hit ✓'); return
    if sl_hit:
        is_trail = (is_buy and active_sl>a['sl']) or (not is_buy and active_sl<a['sl'])
        await close_trade(session,cur,'TRAIL_WIN' if is_trail else 'LOSS',
            f"Trail SL hit — Peak ${a['peak']:,.0f}" if is_trail else 'Stop loss hit'); return

    # Smart exit
    macd_rev = (is_buy and not D['macd_bull']) or (not is_buy and D['macd_bull'])
    htf_rev = (is_buy and not D['htf_bull']) or (not is_buy and D['htf_bull'])
    rsi_ext = (is_buy and D['rsi5']>72) or (not is_buy and D['rsi5']<28)
    bb_ext = (is_buy and D['bb_pos']=='upper') or (not is_buy and D['bb_pos']=='lower')
    rev = sum([macd_rev, htf_rev*2, rsi_ext, bb_ext])

    if profit_pct>=0.15 and rev>=3:
        await close_trade(session,cur,'SMART_EXIT',f'{rev} reversal signals — profit secured'); return
    if profit_pct>=0.2 and rev>=2 and ((is_buy and D['near_res']) or (not is_buy and D['near_sup'])):
        await close_trade(session,cur,'SMART_EXIT','Near S/R + reversals — profit secured'); return
    if profit_pct<0 and abs(profit_pct)>=0.1 and rev>=3 and a['stage']==0:
        await close_trade(session,cur,'EARLY_CUT','Trend reversed — loss minimised'); return

    elapsed = (time.time()-a['time'])/60
    if elapsed>=20 and abs(profit_pct)<0.05 and D['vol_spike']<0.6:
        await close_trade(session,cur,'SMART_EXIT','20min no momentum — exit flat'); return

# ═══════════════════════════════════════════════════
#  CLOSE TRADE
# ═══════════════════════════════════════════════════
async def close_trade(session, exit_price, outcome, reason):
    a = state['active']
    if not a: return
    D = state['D']
    is_buy = a['type']=='BUY'
    dp = (exit_price-a['entry'])/a['entry'] if is_buy else (a['entry']-exit_price)/a['entry']
    pnl = dp*a['pos_size']
    is_win = outcome in ['TP_HIT','TRAIL_WIN','SMART_EXIT'] or (outcome=='MANUAL' and pnl>0)
    elapsed = round((time.time()-a['time'])/60)

    state['paper_bal'] += pnl
    state['trades'].append({
        'type':a['type'],'entry':a['entry'],'exit':exit_price,
        'pnl':round(pnl),'outcome':'WIN' if is_win else 'LOSS',
        'outcome_type':outcome,'lev':a['lev'],'elapsed':elapsed
    })

    wins = sum(1 for t in state['trades'] if t['outcome']=='WIN')
    total = len(state['trades'])
    wr = round(wins/total*100) if total>0 else 0
    total_pnl = sum(t['pnl'] for t in state['trades'])

    labels = {
        'TP_HIT':'🎯 TARGET HIT!','TRAIL_WIN':'📈 TRAILING WIN!',
        'SMART_EXIT':'🧠 SMART EXIT','LOSS':'❌ STOP LOSS','EARLY_CUT':'✂️ EARLY CUT'
    }

    msg = (
        f"{labels.get(outcome,outcome)}\n\n"
        f"{'✅' if is_win else '❌'} <b>{a['type']}</b> · {a['label']} · {a['lev']}x\n"
        f"📍 Entry: ${a['entry']:,.0f}\n"
        f"🚪 Exit: ${exit_price:,.0f}\n"
        f"📊 Move: {dp*100:+.3f}%\n"
        f"⏱ Time: {elapsed} min\n\n"
        f"💰 <b>P&L: {'+'if pnl>=0 else ''}₹{pnl:,.0f}</b>\n"
        f"💳 Balance: ₹{state['paper_bal']:,.0f}\n"
        f"📈 Total: {'+'if total_pnl>=0 else ''}₹{total_pnl:,.0f}\n"
        f"📊 {total} trades · {wr}% win rate\n\n"
        f"💡 {reason}"
    )
    await tg_send(session, msg)
    state['active'] = None
    state['last_sig'] = None

# ═══════════════════════════════════════════════════
#  SCAN
# ═══════════════════════════════════════════════════
async def run_scan(session):
    D = state['D']
    bull, bear = calc_confluence()
    diff = bull-bear

    if D.get('vol_spike',1)<0.15: return
    if D.get('spread_pct',0)>0.08: return

    # CHOPPY MARKET FILTER — flat/sideways market मध्ये NO TRADE
    # Range = high-low / price * 100 — last 20 candles
    p5 = list(state['prices_5m'])
    if len(p5) >= 20:
        rng = (max(p5[-20:]) - min(p5[-20:])) / p5[-1] * 100
        # Less than 0.4% range in 20 candles = choppy = NO TRADE
        if rng < 0.4:
            print(f"Choppy market: range {rng:.2f}% — skip")
            return
        # Price stuck near middle of range = no clear trend = NO TRADE
        mid = (max(p5[-20:]) + min(p5[-20:])) / 2
        dist_from_mid = abs(p5[-1] - mid) / mid * 100
        if dist_from_mid < 0.1 and rng < 0.6:
            print(f"Choppy: price stuck at midrange — skip")
            return

    pos = calc_pos()
    if not pos['can_trade']: return

    htf_buy = D.get('htf_bull',True)

    sig_type = None
    htf_rsi = D.get('htf_rsi', 50)

    # HTF strict filter — RSI must confirm direction
    htf_buy_ok = htf_buy and htf_rsi > 45      # HTF bull + RSI not bearish
    htf_sell_ok = not htf_buy and htf_rsi < 55  # HTF bear + RSI not bullish

    if diff>=MIN_CONF and htf_buy_ok:
        sig_type='BUY'; conf=min(90,50+diff*4)
    elif diff<=-MIN_CONF and htf_sell_ok:
        sig_type='SELL'; conf=min(88,50+abs(diff)*4)
    else:
        return

    if sig_type=='BUY' and not D.get('candle_bull') and D.get('candle_size',0)>0.5: return
    if sig_type=='SELL' and D.get('candle_bull') and D.get('candle_size',0)>0.5: return

    # RSI must confirm direction
    rsi = D.get('rsi5', 50)
    if sig_type=='BUY' and rsi > 68: return   # overbought — don't buy
    if sig_type=='SELL' and rsi < 32: return  # oversold — don't sell

    if state['last_sig'] != sig_type:
        reason = f"Bull:{bull} Bear:{bear} · HTF:{'BULL' if htf_buy else 'BEAR'} · Vol:{D.get('vol_spike',1):.1f}x · {pos['label']}"
        await execute_trade(session, sig_type, conf, reason, pos)

# ═══════════════════════════════════════════════════
#  STATUS
# ═══════════════════════════════════════════════════
async def send_status(session):
    D = state['D']
    wins = sum(1 for t in state['trades'] if t['outcome']=='WIN')
    total = len(state['trades'])
    wr = round(wins/total*100) if total>0 else 0
    total_pnl = sum(t['pnl'] for t in state['trades'])
    bal_chg = (state['paper_bal']-state['paper_start'])/state['paper_start']*100

    a = state['active']
    active_txt = ''
    if a:
        cur = D.get('price',0)
        dp = (cur-a['entry'])/a['entry'] if a['type']=='BUY' else (a['entry']-cur)/a['entry']
        pnl = dp*a['pos_size']
        active_txt = f"\n\n🔄 <b>ACTIVE:</b> {a['type']} · Entry ${a['entry']:,.0f}\nLive P&L: {'+'if pnl>=0 else ''}₹{pnl:,.0f} · Stage {a['stage']}"

    msg = (
        f"📊 <b>BOT STATUS</b> — {datetime.datetime.now().strftime('%H:%M')}\n\n"
        f"💰 Balance: ₹{state['paper_bal']:,.0f} ({bal_chg:+.2f}%)\n"
        f"📈 Total P&L: {'+'if total_pnl>=0 else ''}₹{total_pnl:,.0f}\n"
        f"🎯 {total} trades · {wr}% win rate\n\n"
        f"₿ BTC: ${D.get('price',0):,.0f} ({D.get('chg24',0):+.2f}%)\n"
        f"🌐 HTF: {'BULL 📈' if D.get('htf_bull') else 'BEAR 📉'}\n"
        f"⚡ Bull:{D.get('bull_conf',0)} Bear:{D.get('bear_conf',0)} · RSI:{D.get('rsi5',50)}"
        f"{active_txt}"
    )
    await tg_send(session, msg)

# ═══════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════
async def bot_loop():
    connector = aiohttp.TCPConnector(limit=10)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        print("🚀 BTC Scalper Bot starting...")
        await tg_send(session,
            "🚀 <b>BTC 5M SCALPER BOT STARTED!</b>\n\n"
            "✅ Server running 24/7\n"
            "📊 Scanning every 15 seconds\n"
            "⚡ Auto paper trades — ₹10,000\n"
            "🔒 Smart exit + Trailing SL\n\n"
            "Signals येतील तेव्हा alert येईल!"
        )
        last_status = time.time()
        while True:
            try:
                ok = await fetch_data(session)
                if ok:
                    calc_indicators()
                    if state['active']:
                        await update_active(session)
                    else:
                        await run_scan(session)
                    state['scan_count'] += 1
                    if time.time()-last_status > 1800:
                        await send_status(session)
                        last_status = time.time()
                    if state['scan_count'] % 20 == 0:
                        D = state['D']
                        print(f"#{state['scan_count']} BTC:${D.get('price',0):,.0f} Bull:{D.get('bull_conf',0)} Bear:{D.get('bear_conf',0)} Bal:₹{state['paper_bal']:,.0f}")
            except Exception as e:
                print(f"Error: {e}")
            await asyncio.sleep(FETCH_SEC)

async def main():
    await asyncio.gather(
        start_web(),
        bot_loop()
    )

if __name__ == '__main__':
    asyncio.run(main())

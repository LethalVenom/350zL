#!/usr/bin/env python3
# Enure the OBD environment is installed and accessable, along with ensuring pygames is installed and up to date. 
"""
350Z OBD Dashboard — Final Version
Keys: ESC/Q = quit & save log | C = save abnormal report
      9 (x2) = clear DTC codes | D (hold) = danger to manifold
"""
import obd, pygame, sys, os, csv, math, threading, time as time_mod, random
from datetime import datetime

# ================================================================
# CONFIG
# ================================================================
SCREEN_W, SCREEN_H = 1024, 600
FULLSCREEN         = True

RPM_GREEN  = 500
RPM_YELLOW = 3000
RPM_BLUE   = 5000
RPM_RED    = 6000
RPM_MAX    = 6600
RPM_FIRE   = 3000

GEAR_RATIOS      = {1: 3.541, 2: 2.092, 3: 1.418, 4: 1.000, 5: 0.749}
TIRE_DIAMETER_IN = 26.4
FINAL_DRIVE      = 3.357
TANK_L           = 80.0

LOG_DIR = os.path.expanduser("~/Desktop/obd_logs")

MPH_BLUE_THRESH   = 60
MPH_PURPLE_THRESH = 64

COOLANT_CAUTION_C  = 105
COOLANT_HOT_C      = 110
COOLANT_MAX_C      = 130

# Slip detection: if expected RPM differs from actual by this much → slip
SLIP_RPM_THRESH = 600
SLIP_HOLD_SECS  = 2.0   # how long slip indicator stays bright after event

# ================================================================
# OBD BACKGROUND POLLER
# ================================================================
class OBDPoller(threading.Thread):
    FAST = ['rpm', 'speed']
    SLOW = ['coolant','fuel_rate','fuel_pressure','fuel_trim_short','fuel_trim_long',
            'fuel_trim_short_2','fuel_trim_long_2','maf','throttle','engine_load',
            'intake_temp','run_time','timing_advance','intake_pressure']
    DTC_INT = 10.0

    def __init__(self, conn):
        super().__init__(daemon=True)
        self.conn = conn
        self.lock = threading.Lock()
        raw = {
            'rpm':                   obd.commands.RPM,
            'speed':                 obd.commands.SPEED,
            'coolant':               obd.commands.COOLANT_TEMP,
            'fuel_rate':             obd.commands.FUEL_RATE,
            'fuel_pressure':         obd.commands.FUEL_PRESSURE,
            'fuel_trim_short':       obd.commands.SHORT_FUEL_TRIM_1,
            'fuel_trim_long':        obd.commands.LONG_FUEL_TRIM_1,
            'fuel_trim_short_2':     obd.commands.SHORT_FUEL_TRIM_2  if hasattr(obd.commands,'SHORT_FUEL_TRIM_2') else None,
            'fuel_trim_long_2':      obd.commands.LONG_FUEL_TRIM_2   if hasattr(obd.commands,'LONG_FUEL_TRIM_2')  else None,
            'throttle':              obd.commands.THROTTLE_POS,
            'maf':                   obd.commands.MAF,
            'engine_load':           obd.commands.ENGINE_LOAD         if hasattr(obd.commands,'ENGINE_LOAD')       else None,
            'intake_temp':           obd.commands.INTAKE_TEMP         if hasattr(obd.commands,'INTAKE_TEMP')       else None,
            'intake_pressure':       obd.commands.INTAKE_PRESSURE     if hasattr(obd.commands,'INTAKE_PRESSURE')   else None,
            'timing_advance':        obd.commands.TIMING_ADVANCE      if hasattr(obd.commands,'TIMING_ADVANCE')    else None,
            'run_time':              obd.commands.RUN_TIME             if hasattr(obd.commands,'RUN_TIME')          else None,
        }
        self.supported   = {k:v for k,v in raw.items() if v and conn.supports(v)}
        self.data        = {k:None for k in self.supported}
        self.dtc_codes   = []
        self.mil_status  = '?'
        self.raw_dtcs    = ''
        self._stop       = threading.Event()
        self._slow_idx   = 0
        self._last_dtc   = 0.0
        self._clr_req    = False
        self._clr_result = None

    def _q(self, key):
        cmd = self.supported.get(key)
        if not cmd: return None
        try:
            r = self.conn.query(cmd)
            if r.is_null(): return None
            return r.value.magnitude if hasattr(r.value,'magnitude') else r.value
        except: return None

    def request_clear(self):  self._clr_result = None; self._clr_req = True
    def clear_result(self):   return self._clr_result
    def stop(self):           self._stop.set()

    def run(self):
        slow = [k for k in self.SLOW if k in self.supported]
        while not self._stop.is_set():
            if self._clr_req:
                self._clr_req = False
                try:
                    if self.conn.supports(obd.commands.CLEAR_DTC):
                        self.conn.query(obd.commands.CLEAR_DTC)
                        self._clr_result = True; self._last_dtc = 0
                    else: self._clr_result = False
                except: self._clr_result = False

            fast = {k: self._q(k) for k in self.FAST if k in self.supported}
            sk, sv = None, None
            if slow:
                sk = slow[self._slow_idx % len(slow)]
                sv = self._q(sk)
                self._slow_idx += 1

            dtcs = mil = raw = None
            now = time_mod.monotonic()
            if now - self._last_dtc >= self.DTC_INT:
                self._last_dtc = now
                codes, raw_list, mil_str = [], [], '?'
                try:
                    r = self.conn.query(obd.commands.GET_DTC)
                    if not r.is_null() and r.value:
                        for c,_ in r.value: codes.append(c); raw_list.append(f'STORED:{c}')
                except: pass
                try:
                    if self.conn.supports(obd.commands.GET_PENDING_DTC):
                        r = self.conn.query(obd.commands.GET_PENDING_DTC)
                        if not r.is_null() and r.value:
                            for c,_ in r.value: raw_list.append(f'PENDING:{c}')
                except: pass
                try:
                    if self.conn.supports(obd.commands.STATUS):
                        r = self.conn.query(obd.commands.STATUS)
                        if not r.is_null() and r.value:
                            mil_str = 'ON' if r.value.MIL else 'OFF'
                except: pass
                dtcs, mil, raw = codes, mil_str, ';'.join(raw_list)

            with self.lock:
                for k,v in fast.items(): self.data[k] = v
                if sk: self.data[sk] = sv
                if dtcs is not None:
                    self.dtc_codes = dtcs; self.mil_status = mil; self.raw_dtcs = raw

def get_shared(p):
    with p.lock:
        return dict(p.data), list(p.dtc_codes), p.mil_status, p.raw_dtcs

# ================================================================
# HELPERS
# ================================================================
def calc_gear(speed_mph, rpm):
    if speed_mph < 1 or rpm < 400: return 'N'
    wrpm = (speed_mph * 1056) / (TIRE_DIAMETER_IN * math.pi)
    best, bd = 'N', None
    for g, ratio in GEAR_RATIOS.items():
        d = abs(wrpm * ratio * FINAL_DRIVE - rpm)
        if bd is None or d < bd: bd, best = d, g
    return best

def expected_rpm(speed_mph, gear):
    if gear == 'N' or speed_mph < 1: return None
    ratio = GEAR_RATIOS.get(gear)
    if ratio is None: return None
    return (speed_mph * 1056) / (TIRE_DIAMETER_IN * math.pi) * ratio * FINAL_DRIVE

def save_log(rows, fieldnames):
    if not rows: return None
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    fp = os.path.join(LOG_DIR, f'obd_log_{ts}.csv')
    with open(fp,'w',newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows: w.writerow(r)
    return fp

def save_report(dtc_codes, mil, row, dv):
    home = os.path.expanduser('~')
    ts   = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    fp   = os.path.join(home, f'obd_report_{ts}.txt')
    flags = []
    lines = ['350Z OBD Report', f'Time: {datetime.now().isoformat(timespec="seconds")}',
             '='*50, f'MIL: {mil}']
    if dtc_codes: lines += [f'DTCs: {", ".join(dtc_codes)}']
    else: lines += ['DTCs: none']
    lines += ['', f'RPM: {row.get("rpm")}  Speed: {row.get("speed_mph")} MPH  Gear: {row.get("gear")}']
    c = dv.get('coolant')
    if c is not None:
        cf = c*9/5+32; lines.append(f'Coolant: {cf:.0f}F')
        if c > COOLANT_HOT_C: flags.append(f'Coolant HIGH {cf:.0f}F')
    for k,l in [('fuel_trim_short','FT S B1'),('fuel_trim_long','FT L B1'),
                 ('fuel_trim_short_2','FT S B2'),('fuel_trim_long_2','FT L B2')]:
        v = dv.get(k)
        if v is not None:
            lines.append(f'{l}: {v:+.1f}%')
            if abs(v) > 10: flags.append(f'{l} out of range {v:+.1f}%')
    if mil == 'ON' or dtc_codes: flags.insert(0,'MIL ON or DTCs present')
    lines += ['', 'FLAGGED:' if flags else 'No issues flagged']
    for f in flags: lines.append(f'  ** {f}')
    with open(fp,'w') as fh: fh.write('\n'.join(lines)+'\n')
    return fp, flags

# ================================================================
# PYGAME INIT
# ================================================================
pygame.init()
if FULLSCREEN:
    info = pygame.display.Info()
    SCREEN_W, SCREEN_H = info.current_w, info.current_h
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN)
else:
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
pygame.display.set_caption('350Z OBD')
pygame.mouse.set_visible(False)
clock = pygame.time.Clock()

SX = SCREEN_W / 1024.0; SY = SCREEN_H / 600.0; FS = (SX+SY)/2
def X(v): return int(round(v*SX))
def Y(v): return int(round(v*SY))

def _f(sz, bold=False):
    for n in ['freemono','courier new','couriernew','dejavusansmono','monospace']:
        try: return pygame.font.SysFont(n, int(sz*FS), bold=bold)
        except: pass
    return pygame.font.SysFont('monospace', int(sz*FS), bold=bold)

fHuge  = _f(96, True)
fLarge = _f(48, True)
fMed   = _f(28, True)
fSmall = _f(20)
fTiny  = _f(15)

# ================================================================
# COLORS
# ================================================================
BG     = (5, 5, 8)
PANEL  = (12, 8, 4)
AMBER  = (255, 140, 0)
AMBER_DIM = (70, 40, 8)
WHITE  = (235, 225, 210)
GREEN  = (60, 230, 110)
YELLOW = (255, 220, 60)
BLUE   = (70, 160, 255)
RED    = (255, 45, 30)
ORANGE = (255, 150, 30)
PURPLE = (165, 75, 255)
PURPLE_BOLT = (130, 180, 255)
SEG_ON  = (235, 220, 200)
SEG_OFF = (45, 25, 6)

def lc(a, b, t): return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))
def lerp_color(a, b, t): return lc(a, b, max(0.0, min(1.0, t)))

def rpm_color(rpm):
    if rpm < RPM_GREEN:  return AMBER
    if rpm < RPM_YELLOW: return GREEN
    if rpm < RPM_BLUE:   return YELLOW
    if rpm < RPM_RED:    return BLUE
    return RED
def rpm_col(rpm):
    if rpm < RPM_GREEN:  return AMBER
    if rpm < RPM_YELLOW: return GREEN
    if rpm < RPM_BLUE:   return YELLOW
    if rpm < RPM_RED:    return BLUE
    return RED

# ================================================================
# LAYOUT  (left=RPM strip | right=everything else)
# ================================================================
LW = X(380)   # wider left panel — gives zone labels room to breathe

# RPM vertical bar
RB_X = X(12); RB_Y = Y(10)
RB_W = X(52); RB_H = SCREEN_H - Y(56)
RB   = pygame.Rect(RB_X, RB_Y, RB_W, RB_H)

# Tick column
TK_X = RB_X + RB_W + X(5)
TK_W = X(32)   # just tick marks + numbers, labels go into LABEL zone

# Label zone — POWER/SHIFT/LIMIT sit here, left-aligned
LBL_X = TK_X + TK_W + X(4)
LBL_W = X(68)   # enough for "LIMIT" in fSmall

# Info column: SHIFT alert + RPM digits + trans strip
# Starts after labels
INFO_X = LBL_X + LBL_W + X(4)
INFO_W = LW - INFO_X - X(6)

# SHIFT/LIMIT alert at very top of info column
SHIFT_Y = RB_Y + Y(6)

# Large RPM readout below SHIFT label
BIG_RPM_Y  = SHIFT_Y + Y(28)
BIG_RPM_DW = X(28); BIG_RPM_DH = Y(48); BIG_RPM_SP = X(4)

# Transmission vertical bar below RPM readout
TRANS_BAR_X = INFO_X
TRANS_BAR_Y = BIG_RPM_Y + BIG_RPM_DH + Y(10)
TRANS_BAR_W = INFO_W
TRANS_BAR_H = SCREEN_H - TRANS_BAR_Y - Y(56)
TRANS_RECT  = pygame.Rect(TRANS_BAR_X, TRANS_BAR_Y, TRANS_BAR_W, TRANS_BAR_H)

# Right panel
RP_X = LW + X(6)
RP_W = SCREEN_W - RP_X - X(8)

MPH_R  = pygame.Rect(RP_X, Y(10), RP_W, Y(80))
KPH_R  = pygame.Rect(RP_X, Y(98), RP_W, Y(22))

DG_TOP = Y(128); DG_COL = RP_W//6; DG_ROW = Y(88)

MN_TOP = Y(314); MN_BOT = SCREEN_H - Y(84) - Y(36)
MN_H   = MN_BOT - MN_TOP
MN_W   = RP_W//4 - X(8)

CL_R   = pygame.Rect(RP_X + X(96), SCREEN_H - Y(82), RP_W - X(96), Y(36))
CL_D   = pygame.Rect(RP_X,         SCREEN_H - Y(82), X(92),         Y(36))
DTC_R  = pygame.Rect(X(8),         SCREEN_H - Y(42), SCREEN_W-X(16), Y(34))

MPH_DW = X(28); MPH_DH = Y(40); MPH_SP = X(5)
KPH_DW = X(12); KPH_DH = Y(14); KPH_SP = X(3)

# ================================================================
# SEVEN-SEGMENT RENDERER
# ================================================================
SEV = {
    '0':'abcdef','1':'bc','2':'abdeg','3':'abcdg','4':'bcfg',
    '5':'acdfg','6':'acdefg','7':'abc','8':'abcdefg','9':'abcdfg',
    'N':'ceg','-':'g',' ':'','S':'acdfg',
}

def seg7(surface, ch, x, y, w, h, on, off=SEG_OFF, thick=None):
    segs = SEV.get(str(ch).upper(),'')
    t    = thick or max(3, int(min(w, h//2)*0.16))
    half = h//2
    rects = {
        'a': pygame.Rect(x+t,   y,              w-2*t, t),
        'g': pygame.Rect(x+t,   y+half-t//2,    w-2*t, t),
        'd': pygame.Rect(x+t,   y+h-t,          w-2*t, t),
        'f': pygame.Rect(x,     y+t,             t, half-t),
        'b': pygame.Rect(x+w-t, y+t,             t, half-t),
        'e': pygame.Rect(x,     y+half+t-t//2,   t, half-t),
        'c': pygame.Rect(x+w-t, y+half+t-t//2,   t, half-t),
    }
    for n,r in rects.items():
        pygame.draw.rect(surface, on if n in segs else off, r, border_radius=max(1,t//3))

def seg7s(surface, text, x, y, dw, dh, on, off=SEG_OFF, sp=None):
    sp = sp or max(3, int(dw*0.16))
    cx = x
    for ch in str(text):
        seg7(surface, ch, cx, y, dw, dh, on, off)
        cx += dw + sp
    return cx

# ================================================================
# DRAWING HELPERS
# ================================================================
def brackets(surface, rect, col=None, bcol=None, blen=14, fill=None):
    col  = col  or AMBER_DIM
    bcol = bcol or AMBER
    fill = fill if fill is not None else PANEL
    pygame.draw.rect(surface, fill, rect)
    pygame.draw.rect(surface, col, rect, 1)
    bl = blen
    r  = rect
    for pts in [
        [(r.left,r.top+bl),(r.left,r.top),(r.left+bl,r.top)],
        [(r.right-bl,r.top),(r.right,r.top),(r.right,r.top+bl)],
        [(r.left,r.bottom-bl),(r.left,r.bottom),(r.left+bl,r.bottom)],
        [(r.right-bl,r.bottom),(r.right,r.bottom),(r.right,r.bottom-bl)],
    ]: pygame.draw.lines(surface, bcol, False, pts, 2)

def label(surface, text, x, y, col=AMBER_DIM, font=None):
    f = font or fTiny
    surface.blit(f.render(text.upper(), True, col), (x,y))

# ================================================================
# RPM VERTICAL BAR
# ================================================================
RPM_ZONES = [
    (0,        RPM_GREEN,  AMBER,  ''),
    (RPM_GREEN, RPM_YELLOW, GREEN,  ''),
    (RPM_YELLOW,RPM_BLUE,  YELLOW, 'POWER'),
    (RPM_BLUE,  RPM_RED,   BLUE,   'SHIFT'),
    (RPM_RED,   RPM_MAX,   RED,    'LIMIT'),
]

def draw_rpm_bar(surface, rpm, ticks):
    is_lim  = rpm >= RPM_RED
    is_fire = rpm >= RPM_FIRE
    pulse   = (math.sin(ticks/90.0)+1)/2
    flick   = (math.sin(ticks/32.0)+1)/2
    flick2  = (math.sin(ticks/19.0)+1)/2
    pct     = min(max(rpm/RPM_MAX, 0), 1.0)
    fill_h  = int(RB_H * pct)
    col     = rpm_col(rpm)

    bx,by,bw,bh = RB.x, RB.y, RB.width, RB.height

    # Background
    pygame.draw.rect(surface, PANEL, RB)

    # Segmented fill
    sh = max(3, Y(4)); sg = max(1, Y(2)); st = sh+sg
    ns = bh // st
    for i in range(ns):
        sy2 = by + bh - i*st - sh
        sp  = i/max(1,ns-1); sr = sp*RPM_MAX
        zc  = AMBER_DIM
        for z0,z1,zco,_ in RPM_ZONES:
            if z0 <= sr < z1: zc = tuple(max(0,int(c*0.2)) for c in zco); break
        pygame.draw.rect(surface, zc, pygame.Rect(bx+3,sy2,bw-6,sh), border_radius=2)

    for i in range(ns):
        sy2 = by + bh - i*st - sh
        sp  = i/max(1,ns-1)
        if sp > pct: break
        sr  = sp*RPM_MAX
        sc  = rpm_col(sr)
        if is_lim: sc = lc(RED,(255,200,255),pulse*0.5)
        elif is_fire: sc = tuple(min(255,int(c*(0.88+0.12*flick))) for c in sc)
        pygame.draw.rect(surface, sc, pygame.Rect(bx+3,sy2,bw-6,sh), border_radius=2)

    # Lightning above RPM_FIRE
    if is_fire and pct > 0.02:
        ft = min(1.0,(rpm-RPM_FIRE)/max(1,RPM_MAX-RPM_FIRE))
        bmax = max(0.06, min(0.97,pct))
        # outer glow
        for gi in range(4,0,-1):
            exp = int(bw*0.18*gi*(0.45+0.55*flick))
            gc  = lc(AMBER_DIM, col, gi/4*ft)
            gc  = tuple(min(255,int(c*(0.3+0.7*flick))) for c in gc)
            gr  = pygame.Rect(bx-exp, by+bh-fill_h, bw+2*exp, fill_h)
            pygame.draw.rect(surface, gc, gr, width=max(1,exp//2))
        # bolts
        for _ in range(max(2,int(5*ft*(0.4+0.6*flick2)))):
            bp   = random.uniform(0.05, bmax)
            by0  = int(by+bh-bp*bh)
            blen2 = random.randint(X(12),X(32))
            bsegs = random.randint(3,7)
            dirn  = random.choice([-1,1])
            pts   = [(bx if dirn<0 else bx+bw, by0)]
            bcx,bcy = pts[0]
            for _ in range(bsegs):
                bcx = int(bcx+dirn*blen2/bsegs)
                bcy = int(bcy+random.randint(-Y(7),Y(7)))
                pts.append((bcx,bcy))
            bc = lc(col,(230,240,255),0.5+0.5*random.random())
            if len(pts)>1:
                pygame.draw.lines(surface,bc,False,pts,2)
                pygame.draw.circle(surface,WHITE,pts[-1],2)
        # sparks
        for _ in range(int(5*ft*flick2)):
            sx2 = random.randint(bx-X(6),bx+bw+X(6))
            sy2 = int(by+bh-pct*bh)+random.randint(-Y(8),Y(8))
            pygame.draw.circle(surface,lc(col,(210,235,255),random.random()),(sx2,sy2),random.randint(1,3))

    # Border (no fill — drawn last so it overlays segments)
    bcol2 = RED if is_lim else AMBER
    bl2   = X(10)
    bw2   = max(2, int((2+pulse*4)*FS)) if is_lim else 1
    pygame.draw.rect(surface, bcol2, RB, width=bw2)
    for pts in [
        [(bx,by+bl2),(bx,by),(bx+bl2,by)],
        [(bx+bw-bl2,by),(bx+bw,by),(bx+bw,by+bl2)],
        [(bx,by+bh-bl2),(bx,by+bh),(bx+bl2,by+bh)],
        [(bx+bw-bl2,by+bh),(bx+bw,by+bh),(bx+bw,by+bh-bl2)],
    ]: pygame.draw.lines(surface, WHITE if is_lim else AMBER, False, pts, 2)

    # Zone boundary lines on bar face
    for z0,z1,zco,_ in RPM_ZONES:
        if z0 == 0: continue
        ty = int(by+bh-(z0/RPM_MAX)*bh)
        pygame.draw.line(surface, zco, (bx,ty),(bx+bw,ty),1)

    # Ticks (right of bar)
    for rt in range(0,RPM_MAX+1,500):
        tp   = rt/RPM_MAX
        ty   = int(by+bh-tp*bh)
        maj  = (rt%1000==0)
        tcol = WHITE if maj else AMBER_DIM
        tlen = X(10) if maj else X(5)
        pygame.draw.line(surface,tcol,(TK_X,ty),(TK_X+tlen,ty),2 if maj else 1)
        if maj:
            lt = fTiny.render(str(rt//1000), True, WHITE)
            surface.blit(lt,(TK_X+X(13), ty-lt.get_height()//2))

    # Zone labels — own dedicated column, left-aligned, never overlap RPM panel
    for z0,z1,zco,zlbl in RPM_ZONES:
        if not zlbl: continue
        mid_y = int(by+bh-((z0+z1)/2/RPM_MAX)*bh)
        lt    = fSmall.render(zlbl, True, zco)
        surface.blit(lt,(LBL_X, mid_y-lt.get_height()//2))

    # Shift / limit label — top of info column, clear of tick marks
    lbl2 = ''
    if rpm >= RPM_RED:    lbl2 = 'LIMITER!'
    elif rpm >= RPM_BLUE: lbl2 = 'SHIFT'
    if lbl2:
        lc2 = lc(RED,(255,200,255),pulse*0.6) if is_lim else BLUE
        lt  = fSmall.render(lbl2, True, lc2)
        surface.blit(lt,(INFO_X + INFO_W//2 - lt.get_width()//2, SHIFT_Y))

# ================================================================
# LARGE RPM READOUT (replaces gear digit)
# ================================================================
def draw_rpm_large(surface, rpm, ticks):
    """Big four-digit RPM readout sitting cleanly between shift label and trans strip."""
    is_lim  = rpm >= RPM_RED
    pulse   = (math.sin(ticks / 90.0) + 1) / 2
    col     = rpm_color(rpm)

    # Subtle background panel
    panel_r = pygame.Rect(INFO_X - X(4), BIG_RPM_Y - Y(4),
                          INFO_W + X(8), BIG_RPM_DH + Y(8))
    pygame.draw.rect(surface, PANEL, panel_r, border_radius=4)
    pygame.draw.rect(surface, AMBER_DIM, panel_r, width=1, border_radius=4)

    # Ghost "8888" unlit segments
    seg7s(surface, '8888', INFO_X, BIG_RPM_Y,
          BIG_RPM_DW, BIG_RPM_DH, SEG_OFF, SEG_OFF, BIG_RPM_SP)

    # Live digits in zone color
    on_col = lerp_color(RED, (255,200,255), pulse*0.5) if is_lim else col
    seg7s(surface, f'{int(rpm):04d}', INFO_X, BIG_RPM_Y,
          BIG_RPM_DW, BIG_RPM_DH, on_col, PANEL, BIG_RPM_SP)

    # RPM label below digits
    rl = fTiny.render('RPM', True, AMBER_DIM)
    surface.blit(rl, (INFO_X + (4*BIG_RPM_DW + 3*BIG_RPM_SP)//2 - rl.get_width()//2,
                       BIG_RPM_Y + BIG_RPM_DH + Y(2)))

# ================================================================
# TRANSMISSION STRIP — vertical bar, top=1 bottom=5, S markers between
# ================================================================
def draw_trans_strip(surface, gear, slip_bright):
    brackets(surface, TRANS_RECT, col=AMBER_DIM, bcol=AMBER, blen=X(8), fill=PANEL)

    bx = TRANS_RECT.x + X(6)
    by = TRANS_RECT.y + Y(6)
    bw = TRANS_RECT.width - X(12)
    bh = TRANS_RECT.height - Y(12)

    # 5 gears + 4 S slots = 9 rows
    # Distribute evenly top-to-bottom: 1 at top, 5 at bottom
    n_items = 9   # 5 gears + 4 S markers
    slot_h  = bh / n_items

    # Center line
    cx = bx + bw // 2
    pygame.draw.line(surface, AMBER_DIM, (cx, by), (cx, by + bh), 1)

    gear_positions = {}  # gear_num -> center_y for the active indicator

    for idx in range(n_items):
        cy2 = int(by + (idx + 0.5) * slot_h)
        is_gear_slot = (idx % 2 == 0)
        gear_num     = (idx // 2) + 1   # 1,2,3,4,5
        slip_idx     = idx // 2          # 0=1-2, 1=2-3, etc

        if is_gear_slot:
            active   = (gear == gear_num)
            on_col   = WHITE if active else AMBER_DIM
            off_col  = PANEL
            dw, dh   = X(17), Y(28)   # ~30% narrower than X(24)
            gx       = cx - dw // 2
            gy       = int(cy2 - dh // 2)
            seg7(surface, str(gear_num), gx, gy, dw, dh, on_col, off_col)
            if active:
                ar = pygame.Rect(gx - X(4), gy - Y(3), dw + X(8), dh + Y(6))
                pygame.draw.rect(surface, AMBER, ar, width=1, border_radius=2)
            gear_positions[gear_num] = cy2
        else:
            # S slip marker — smaller than gear digits
            bright   = slip_bright[slip_idx - 1] if 0 < slip_idx <= len(slip_bright) else 0
            r_val    = int(80 + 175 * bright)
            g_val    = int(10 + 10  * bright)
            slip_col = (r_val, g_val, 10)
            dw, dh   = X(10), Y(15)
            sx2      = cx - dw // 2
            sy2      = int(cy2 - dh // 2)
            seg7(surface, 'S', sx2, sy2, dw, dh, slip_col, PANEL)

    # Label
    lt = fTiny.render('TRANS', True, AMBER_DIM)
    surface.blit(lt, (TRANS_RECT.centerx - lt.get_width()//2,
                      TRANS_RECT.bottom - lt.get_height() - Y(2)))

# ================================================================
# SPEED BARS
# ================================================================
def draw_speed(surface, mph, kph, ticks):
    bc = PURPLE if mph > MPH_PURPLE_THRESH else (BLUE if mph < MPH_BLUE_THRESH else AMBER)

    # MPH bar
    brackets(surface, MPH_R, col=AMBER_DIM, bcol=AMBER, blen=X(10), fill=PANEL)
    inner = MPH_R.inflate(-6,-6)
    fw    = int(inner.width * min(mph/160,1.0))
    if fw > 2:
        sx2 = inner.x
        while sx2 < inner.x + fw:
            sw = min(X(8), inner.x+fw-sx2)
            pygame.draw.rect(surface, bc, pygame.Rect(sx2,inner.y,sw,inner.height))
            sx2 += X(8)+X(3)
    if mph > MPH_PURPLE_THRESH:
        # lightning along bar edges
        segs = 8; sw2 = MPH_R.width/segs
        for ey,sign in [(MPH_R.top,-1),(MPH_R.bottom,1)]:
            pts = []
            for i in range(segs+1):
                jitter = int(math.sin(ticks/140.0+i*1.4)*Y(4))
                pts.append((MPH_R.left+int(i*sw2), ey+sign*abs(jitter)))
            pygame.draw.lines(surface,PURPLE_BOLT,False,pts,2)

    total_dw = 3*MPH_DW + 2*MPH_SP
    dx = MPH_R.x + X(12)
    dy = MPH_R.centery - MPH_DH//2
    seg7s(surface, f'{mph:03.0f}', dx, dy, MPH_DW, MPH_DH, SEG_ON, SEG_OFF, MPH_SP)
    surface.blit(fTiny.render('MPH',True,AMBER_DIM),(dx+total_dw+X(6), MPH_R.centery-fTiny.get_height()//2))

    # KPH bar (thin)
    pygame.draw.rect(surface, PANEL, KPH_R)
    pygame.draw.rect(surface, AMBER_DIM, KPH_R, 1)
    ki = KPH_R.inflate(-4,-4)
    kw = int(ki.width * min(kph/260,1.0))
    if kw > 2: pygame.draw.rect(surface, AMBER_DIM, pygame.Rect(ki.x,ki.y,kw,ki.height))
    kdx = KPH_R.x + X(8)
    kdy = KPH_R.centery - KPH_DH//2
    seg7s(surface, f'{kph:03.0f}', kdx, kdy, KPH_DW, KPH_DH, AMBER_DIM, SEG_OFF, KPH_SP)
    ku  = fTiny.render('KPH',True,AMBER_DIM)
    surface.blit(ku,(kdx+3*KPH_DW+2*KPH_SP+X(5), KPH_R.centery-ku.get_height()//2))

# ================================================================
# DATA CELLS
# ================================================================
def cell(surface, col_i, row_i, lbl2, val, unit='', warn=False):
    x  = RP_X + col_i*DG_COL
    y  = DG_TOP + row_i*DG_ROW
    r  = pygame.Rect(x, y, DG_COL-X(6), DG_ROW-Y(6))
    has_val  = val is not None
    bc       = ORANGE if warn else (AMBER if has_val else AMBER_DIM)
    brackets(surface, r, col=bc, bcol=bc, blen=X(8), fill=PANEL)
    # Label is full value brightness when active, dim when no reading
    lbl_col  = (ORANGE if warn else AMBER) if has_val else AMBER_DIM
    label(surface, lbl2, r.x+X(8), r.y+Y(5), col=lbl_col)
    vstr = f'{val}{unit}' if has_val else '---'
    vc   = ORANGE if warn else AMBER
    vt   = fMed.render(vstr, True, vc)
    surface.blit(vt,(r.x+X(8), r.y+Y(26)))

# ================================================================
# MINI VERTICAL BARS
# ================================================================
def draw_mini_bars(surface, throttle, load, timing, maf):
    defs = [
        ('THROTTLE','%',    throttle,  0,  100, AMBER),
        ('ENG LOAD','%',    load,      0,  100, GREEN),
        ('TIMING',  '°',    timing,  -10,   45, YELLOW),
        ('AIR FLOW','g/s',  maf,       0,   30, ORANGE),
    ]
    total = len(defs)*MN_W + (len(defs)-1)*X(8)
    sx2   = RP_X + (RP_W - total)//2
    lbl_h = fTiny.get_height()*2 + Y(4)

    for lbl2, unit, val, lo, hi, col in defs:
        bx2 = sx2; by2 = MN_TOP; bw2 = MN_W; bh2 = MN_H
        pygame.draw.rect(surface, PANEL, pygame.Rect(bx2,by2,bw2,bh2))
        pygame.draw.rect(surface, AMBER_DIM, pygame.Rect(bx2,by2,bw2,bh2),1)
        if val is not None:
            rng  = hi-lo
            pct2 = min(max((val-lo)/rng,0),1)
            fh   = int(bh2*pct2)
            if fh>2: pygame.draw.rect(surface,col,pygame.Rect(bx2,by2+bh2-fh,bw2,fh))
            vt = fSmall.render(f'{val:.0f}',True,WHITE)
            vy = by2+bh2-fh-vt.get_height()-Y(3) if fh>Y(24) else by2+Y(5)
            surface.blit(vt,(bx2+bw2//2-vt.get_width()//2,vy))
        else:
            nt = fSmall.render('--', True, AMBER_DIM)
            # center in the bar area (excluding label space below)
            surface.blit(nt,(bx2+bw2//2-nt.get_width()//2, by2+bh2//2-nt.get_height()//2))
        # corner ticks
        for pts in [
            [(bx2,by2+X(5)),(bx2,by2),(bx2+X(5),by2)],
            [(bx2+bw2-X(5),by2),(bx2+bw2,by2),(bx2+bw2,by2+X(5))],
            [(bx2,by2+bh2-X(5)),(bx2,by2+bh2),(bx2+X(5),by2+bh2)],
            [(bx2+bw2-X(5),by2+bh2),(bx2+bw2,by2+bh2),(bx2+bw2,by2+bh2-X(5))],
        ]: pygame.draw.lines(surface,col,False,pts,2)
        # labels
        lt1 = fTiny.render(lbl2,True,col)
        lt2 = fTiny.render(unit,True,AMBER_DIM)
        surface.blit(lt1,(bx2+bw2//2-lt1.get_width()//2, by2+bh2+Y(3)))
        surface.blit(lt2,(bx2+bw2//2-lt2.get_width()//2, by2+bh2+Y(3)+lt1.get_height()))
        sx2 += MN_W + X(8)

# ================================================================
# COOLANT BAR
# ================================================================
def draw_coolant(surface, coolant_c, ticks):
    is_hot = coolant_c is not None and coolant_c >= COOLANT_HOT_C
    pulse  = (math.sin(ticks/90.0)+1)/2
    col    = RED if is_hot else (YELLOW if coolant_c and coolant_c>=COOLANT_CAUTION_C else (100,180,255))
    bcol   = RED if is_hot else AMBER_DIM

    brackets(surface, CL_R, col=bcol, bcol=bcol, blen=X(8), fill=PANEL)
    inner = CL_R.inflate(-6,-6)
    if coolant_c is not None:
        fw = int(inner.width*min(max(coolant_c/COOLANT_MAX_C,0),1))
        if fw > 2:
            sx2 = inner.x
            while sx2 < inner.x+fw:
                sw = min(X(8), inner.x+fw-sx2)
                pygame.draw.rect(surface,col,pygame.Rect(sx2,inner.y,sw,inner.height))
                sx2 += X(8)+X(3)
    for thresh in [COOLANT_CAUTION_C, COOLANT_HOT_C]:
        tx = inner.x+int(inner.width*(thresh/COOLANT_MAX_C))
        pygame.draw.line(surface,AMBER,(tx,CL_R.top-Y(2)),(tx,CL_R.top),2)
        pygame.draw.line(surface,AMBER,(tx,CL_R.bottom),(tx,CL_R.bottom+Y(2)),2)
    if is_hot:
        lc3 = lc(RED,(255,200,200),pulse*0.6)
        lt  = fSmall.render('OVERHEATING',True,lc3)
        surface.blit(lt,(CL_R.right-lt.get_width()-X(10),CL_R.centery-lt.get_height()//2))

    # Digit panel
    pygame.draw.rect(surface,PANEL,CL_D)
    pygame.draw.rect(surface,AMBER_DIM,CL_D,1)
    if coolant_c is not None:
        cf  = coolant_c*9/5+32
        vt  = fSmall.render(f'{cf:.0f}F',True,RED if is_hot else WHITE)
    else:
        vt  = fSmall.render('---F',True,AMBER_DIM)
    surface.blit(vt,(CL_D.centerx-vt.get_width()//2, CL_D.centery-vt.get_height()//2-Y(3)))
    lt = fTiny.render('COOLANT',True,AMBER_DIM)
    surface.blit(lt,(CL_D.centerx-lt.get_width()//2, CL_D.bottom-lt.get_height()-Y(2)))

# ================================================================
# DTC BAR
# ================================================================
def draw_dtc(surface, codes, mil):
    bad = mil=='ON' or codes
    bc  = RED if bad else AMBER_DIM
    brackets(surface,DTC_R,col=bc,bcol=bc,blen=X(8),fill=PANEL)
    txt = f'WARNING: {", ".join(codes) if codes else "MIL ON"}' if bad else 'SYSTEMS NOMINAL  //  NO ACTIVE DTC  //  MIL OFF'
    lt  = fSmall.render(txt,True,bc)
    surface.blit(lt,(DTC_R.x+X(12),DTC_R.centery-lt.get_height()//2))

# ================================================================
# STARTUP SCREEN
# ================================================================
def draw_startup(surface, conn, poller, progress):
    surface.fill(BG)
    lt = fLarge.render('350Z  OBD  DASHBOARD', True, AMBER)
    surface.blit(lt,(SCREEN_W//2-lt.get_width()//2, Y(30)))

    proto = conn.protocol_name() if conn.status()==obd.OBDStatus.CAR_CONNECTED else 'No ECU'
    pid   = conn.protocol_id()   if conn.status()==obd.OBDStatus.CAR_CONNECTED else '-'
    port  = conn.port_name() or '-'

    infos = [('PORT', port),('PROTOCOL', f'{pid}  {proto}'),
             ('STATUS', str(conn.status())),('SUPPORTED PIDs', str(len(poller.supported)))]
    y0 = Y(110)
    for lbl2,val in infos:
        lt = fSmall.render(f'{lbl2}:', True, AMBER_DIM)
        vt = fSmall.render(val, True, WHITE)
        surface.blit(lt,(X(60),y0)); surface.blit(vt,(X(280),y0))
        y0 += Y(38)

    label(surface,'AVAILABLE PIDs',X(60),y0,col=AMBER_DIM)
    y0 += Y(24)
    keys = list(poller.supported.keys())
    cw   = (SCREEN_W-X(120))//4
    for i,k in enumerate(keys):
        pt = fTiny.render(k,True,(150,100,40))
        surface.blit(pt,(X(60)+(i%4)*cw, y0+(i//4)*Y(20)))

    br = pygame.Rect(X(60),SCREEN_H-Y(55),SCREEN_W-X(120),Y(18))
    pygame.draw.rect(surface,PANEL,br)
    pygame.draw.rect(surface,AMBER_DIM,br,1)
    fw = int(br.width*progress)
    if fw>2: pygame.draw.rect(surface,AMBER,pygame.Rect(br.x,br.y,fw,br.height))
    lt = fTiny.render('PRESS ANY KEY TO CONTINUE  //  AUTO IN 5s',True,AMBER_DIM)
    surface.blit(lt,(SCREEN_W//2-lt.get_width()//2,SCREEN_H-Y(30)))

# ================================================================
# MAIN
# ================================================================
def main():
    connection = obd.OBD()
    poller     = OBDPoller(connection)
    poller.start()

    # Startup screen
    t0 = time_mod.monotonic()
    while True:
        elapsed = time_mod.monotonic()-t0
        draw_startup(screen, connection, poller, min(elapsed/5.0,1.0))
        pygame.display.flip()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: poller.stop(); pygame.quit(); sys.exit()
            if ev.type == pygame.KEYDOWN: elapsed = 6
        if elapsed >= 5.0: break
        clock.tick(30)

    # CSV fieldnames
    fieldnames = ['timestamp','gear','rpm','speed_mph','speed_kph',
                  'coolant_c','fuel_rate','fuel_pressure',
                  'fuel_trim_short','fuel_trim_long','fuel_trim_short_2','fuel_trim_long_2',
                  'maf','throttle','engine_load','intake_temp_c','run_time_s',
                  'timing_advance','intake_pressure','tank_liters','active_dtcs']

    # State
    log_rows      = []
    tank_l        = 20.0        # start conservative — shows --- until fuel_rate arrives
    tank_updating = False
    last_tank_t   = time_mod.monotonic()
    rt_base       = None
    rt_base_t     = 0.0

    data_vals   = {}
    dtc_codes   = []
    mil_status  = '?'
    raw_dtcs    = ''
    rpm         = 0.0
    speed_kph   = 0.0
    gear        = 'N'
    row         = {}

    disp_rpm   = 0.0
    disp_kph   = 0.0

    # Slip detection: 4 slip brightness values (between gears 1-2, 2-3, 3-4, 4-5)
    slip_bright    = [0.0, 0.0, 0.0, 0.0]
    slip_last_time = [0.0, 0.0, 0.0, 0.0]

    status_msg       = ''
    status_msg_until = 0
    clr_armed        = False
    clr_armed_until  = 0
    clr_pending      = False
    danger_held      = False

    running = True
    while running:
        ticks = pygame.time.get_ticks()
        dt    = clock.get_time()/1000.0

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT: running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q): running = False
                elif ev.key == pygame.K_9:
                    nm = pygame.time.get_ticks()
                    if clr_armed and nm < clr_armed_until:
                        clr_armed   = False
                        clr_pending = True
                        poller.request_clear()
                        status_msg       = 'CLEARING DTC CODES...'
                        status_msg_until = nm+4000
                    else:
                        clr_armed       = True
                        clr_armed_until = nm+3000
                        status_msg       = 'PRESS 9 AGAIN TO CLEAR DTC CODES'
                        status_msg_until = clr_armed_until
                elif ev.key == pygame.K_c:
                    fp,flags = save_report(dtc_codes,mil_status,row,data_vals)
                    status_msg       = f'REPORT SAVED ({len(flags)} FLAGGED)' if flags else 'REPORT SAVED - NOMINAL'
                    status_msg_until = ticks+2500
                elif ev.key == pygame.K_d:
                    danger_held = True
            elif ev.type == pygame.KEYUP:
                if ev.key == pygame.K_d: danger_held = False

        if clr_pending:
            r2 = poller.clear_result()
            if r2 is not None:
                clr_pending      = False
                status_msg       = 'DTC CODES CLEARED' if r2 else 'CLEAR DTC NOT SUPPORTED'
                status_msg_until = ticks+2500

        # Get data from poller
        data_vals, dtc_codes, mil_status, raw_dtcs = get_shared(poller)

        rpm       = data_vals.get('rpm')   or 0
        speed_kph = data_vals.get('speed') or 0
        speed_mph = speed_kph * 0.621371

        # Smooth display values
        alpha    = min(1.0, 5.0*dt)
        disp_rpm += (rpm      - disp_rpm) * alpha
        disp_kph += (speed_kph - disp_kph) * alpha
        if abs(disp_rpm - rpm) < 8:        disp_rpm = float(rpm)
        if abs(disp_kph - speed_kph) < 0.3: disp_kph = float(speed_kph)
        disp_mph = disp_kph * 0.621371

        gear = calc_gear(speed_mph, rpm)

        # Tank estimate
        fr = data_vals.get('fuel_rate')
        if fr is not None:
            tank_updating = True
            nm2   = time_mod.monotonic()
            eh    = (nm2-last_tank_t)/3600.0
            tank_l = max(0, min(TANK_L, tank_l - fr*eh))
            last_tank_t = nm2

        # RTTS
        rt = data_vals.get('run_time')
        if rt is not None: rt_base = rt; rt_base_t = time_mod.monotonic()

        # Slip detection
        now_m = time_mod.monotonic()
        if isinstance(gear, int) and speed_mph > 2 and rpm > 400:
            exp = expected_rpm(speed_mph, gear)
            if exp is not None:
                diff = abs(rpm - exp)
                if diff > SLIP_RPM_THRESH:
                    # Which slip indicator? Between prev and current gear
                    for si in range(4):
                        if gear == si+1 or gear == si+2:
                            slip_last_time[si] = now_m
                            slip_bright[si]    = 1.0
        # Decay slip brightness
        for si in range(4):
            elapsed_slip = now_m - slip_last_time[si]
            slip_bright[si] = max(0.0, 1.0 - elapsed_slip/SLIP_HOLD_SECS)

        # Build log row
        row = {
            'timestamp':  datetime.now().isoformat(timespec='seconds'),
            'gear':       gear, 'rpm': round(rpm,0),
            'speed_mph':  round(speed_mph,1), 'speed_kph': round(speed_kph,1),
            'tank_liters': round(tank_l,2) if tank_updating else '',
            'active_dtcs': raw_dtcs,
        }
        for k,fk in [('coolant','coolant_c'),('fuel_rate','fuel_rate'),
                      ('fuel_pressure','fuel_pressure'),
                      ('fuel_trim_short','fuel_trim_short'),('fuel_trim_long','fuel_trim_long'),
                      ('fuel_trim_short_2','fuel_trim_short_2'),('fuel_trim_long_2','fuel_trim_long_2'),
                      ('maf','maf'),('throttle','throttle'),('engine_load','engine_load'),
                      ('intake_temp','intake_temp_c'),('run_time','run_time_s'),
                      ('timing_advance','timing_advance'),('intake_pressure','intake_pressure')]:
            v = data_vals.get(k)
            if v is not None: row[fk] = round(v,2)
        log_rows.append(row)

        def v(k): return data_vals.get(k)

        # ---- DRAW ----
        screen.fill(BG)

        # Separator
        pygame.draw.line(screen, AMBER_DIM, (LW, Y(8)), (LW, SCREEN_H-Y(8)), 1)
        pygame.draw.line(screen, AMBER_DIM, (X(8), Y(6)), (SCREEN_W-X(8), Y(6)), 1)

        draw_rpm_bar(screen, disp_rpm, ticks)
        draw_rpm_large(screen, disp_rpm, ticks)
        draw_trans_strip(screen, gear, slip_bright)
        draw_speed(screen, disp_mph, disp_kph, ticks)

        # Data grid 6x2
        def w(k,lo,hi): return v(k) is not None and abs(v(k)) > max(abs(lo),abs(hi))*0.9
        cell(screen,0,0,'LOAD',        f'{v("engine_load"):.0f}'        if v('engine_load')    is not None else None,'%')
        cell(screen,1,0,'INTAKE AIR',  f'{v("intake_temp")*9/5+32:.0f}' if v('intake_temp')    is not None else None,'F')
        cell(screen,2,0,'MAP',         f'{v("intake_pressure"):.0f}'    if v('intake_pressure') is not None else None,' kPa')
        cell(screen,3,0,'TIMING',      f'{v("timing_advance"):+.1f}'    if v('timing_advance') is not None else None,'°')
        cell(screen,4,0,'THROTTLE',    f'{v("throttle"):.0f}'           if v('throttle')       is not None else None,'%')
        cell(screen,5,0,'TANK EST',    f'{tank_l:.1f}L'                  if tank_updating       else None)
        cell(screen,0,1,'TRIM S B1',   f'{v("fuel_trim_short"):+.1f}'   if v('fuel_trim_short') is not None else None,'%',
             warn=v('fuel_trim_short') is not None and abs(v('fuel_trim_short'))>10)
        cell(screen,1,1,'TRIM L B1',   f'{v("fuel_trim_long"):+.1f}'    if v('fuel_trim_long')  is not None else None,'%',
             warn=v('fuel_trim_long')  is not None and abs(v('fuel_trim_long'))>10)
        cell(screen,2,1,'TRIM S B2',   f'{v("fuel_trim_short_2"):+.1f}' if v('fuel_trim_short_2') is not None else None,'%',
             warn=v('fuel_trim_short_2') is not None and abs(v('fuel_trim_short_2'))>10)
        cell(screen,3,1,'TRIM L B2',   f'{v("fuel_trim_long_2"):+.1f}'  if v('fuel_trim_long_2')  is not None else None,'%',
             warn=v('fuel_trim_long_2')  is not None and abs(v('fuel_trim_long_2'))>10)
        cell(screen,4,1,'MAF',         f'{v("maf"):.1f}'                if v('maf')            is not None else None,' g/s')
        cell(screen,5,1,'FUEL PRES',   f'{v("fuel_pressure"):.0f}'      if v('fuel_pressure')  is not None else None,' kPa')

        draw_mini_bars(screen, v('throttle'), v('engine_load'), v('timing_advance'), v('maf'))
        draw_coolant(screen, v('coolant'), ticks)
        draw_dtc(screen, dtc_codes, mil_status)

        # RTTS
        if rt_base is not None:
            rtts = rt_base + (time_mod.monotonic()-rt_base_t)
            h2,rem = divmod(int(rtts),3600); m2,s2 = divmod(rem,60)
            rt_t = fTiny.render(f'RTTS {h2:02d}:{m2:02d}:{s2:02d}',True,AMBER_DIM)
            screen.blit(rt_t,(SCREEN_W-X(12)-rt_t.get_width(), Y(8)))

        # Status overlay
        if ticks < status_msg_until:
            mt = fSmall.render(status_msg,True,AMBER)
            mr = pygame.Rect(SCREEN_W//2-mt.get_width()//2-X(16),Y(18),mt.get_width()+X(32),Y(34))
            pygame.draw.rect(screen,PANEL,mr)
            pygame.draw.rect(screen,AMBER,mr,1)
            screen.blit(mt,(mr.centerx-mt.get_width()//2, mr.centery-mt.get_height()//2))

        # Danger overlay
        if danger_held:
            flash = (ticks//200)%2==0
            if flash:
                ov = pygame.Surface((SCREEN_W,SCREEN_H)); ov.fill(RED); ov.set_alpha(100)
                screen.blit(ov,(0,0))
            wt = fLarge.render('DANGER TO MANIFOLD',True,YELLOW)
            wr = pygame.Rect(SCREEN_W//2-wt.get_width()//2-X(20),SCREEN_H//2-wt.get_height()//2-Y(12),
                             wt.get_width()+X(40),wt.get_height()+Y(24))
            pygame.draw.rect(screen, RED if flash else BG, wr)
            screen.blit(wt,(wr.centerx-wt.get_width()//2, wr.centery-wt.get_height()//2))

        pygame.display.flip()
        clock.tick(30)

    poller.stop()
    fp = save_log(log_rows, fieldnames)
    pygame.quit(); connection.close()
    if fp: print(f'Saved {len(log_rows)} rows → {fp}')
    sys.exit()

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: pygame.quit(); sys.exit()

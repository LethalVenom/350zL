#!/usr/bin/env python3
"""
350Z Shift Light Display
- RPM polled every frame (fast)
- Intake temp / Timing / Coolant polled every 60s (slow, background)
- Flame shift indicator center-right
- Data table bottom-left
"""
import obd, pygame, sys, math, random, threading, time as time_mod
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────
SCREEN_W, SCREEN_H = 1024, 600
FULLSCREEN         = True

RPM_BLUE_START  = 3000   # dark blue flame starts
RPM_GREEN_START = 5000   # light green flame
RPM_BLINK_START = 5500   # begins blinking
RPM_RED_LINE    = 6600   # solid red
RPM_MAX         = 7000
SLOW_INTERVAL   = 60.0   # seconds between slow PID polls

# ── COLORS ──────────────────────────────────────────────────────
BLACK     = (4,   6,  10)
PANEL     = (10,  10, 18)
DIM       = (40,  38, 30)
AMBER     = (255, 145, 0)
AMBER_DIM = (70,  45,  8)
WHITE     = (230, 225, 215)
BLUE_DRK  = (20,  40, 180)
BLUE_LT   = (80, 120, 255)
GREEN_LT  = (80, 255, 120)
RED_FLAME = (255,  40,  20)
ORANGE    = (255, 130,  20)
YELLOW    = (255, 220,  40)

# ── OBD POLLER ─────────────────────────────────────────────────
class Poller(threading.Thread):
    def __init__(self, conn):
        super().__init__(daemon=True)
        self.conn = conn
        self.lock = threading.Lock()
        self._stop = threading.Event()

        cmds = {
            'rpm':     obd.commands.RPM,
            'coolant': obd.commands.COOLANT_TEMP,
            'timing':  obd.commands.TIMING_ADVANCE  if hasattr(obd.commands,'TIMING_ADVANCE')  else None,
            'intake':  obd.commands.INTAKE_TEMP      if hasattr(obd.commands,'INTAKE_TEMP')      else None,
        }
        self.supported = {k:v for k,v in cmds.items() if v and conn.supports(v)}
        self.data      = {k:None for k in self.supported}
        self._last_slow = 0.0

    def _q(self, key):
        cmd = self.supported.get(key)
        if not cmd: return None
        try:
            r = self.conn.query(cmd)
            if r.is_null(): return None
            return r.value.magnitude if hasattr(r.value,'magnitude') else r.value
        except: return None

    def stop(self): self._stop.set()

    def run(self):
        while not self._stop.is_set():
            # Fast: RPM every iteration
            rpm = self._q('rpm')
            with self.lock:
                if rpm is not None:
                    self.data['rpm'] = rpm

            # Slow: everything else every SLOW_INTERVAL seconds
            now = time_mod.monotonic()
            if now - self._last_slow >= SLOW_INTERVAL:
                self._last_slow = now
                slow_vals = {}
                for key in ('coolant','timing','intake'):
                    slow_vals[key] = self._q(key)
                with self.lock:
                    for k,v in slow_vals.items():
                        if v is not None:
                            self.data[k] = v

    def snapshot(self):
        with self.lock:
            return dict(self.data)

# ── PYGAME ──────────────────────────────────────────────────────
pygame.init()
if FULLSCREEN:
    info = pygame.display.Info()
    SCREEN_W, SCREEN_H = info.current_w, info.current_h
screen = pygame.display.set_mode(
    (SCREEN_W, SCREEN_H),
    pygame.FULLSCREEN if FULLSCREEN else 0
)
pygame.display.set_caption('Shift Light')
pygame.mouse.set_visible(False)
clock = pygame.time.Clock()

SX = SCREEN_W / 1024.0
SY = SCREEN_H / 600.0
FS = (SX+SY)/2.0
def X(v): return int(round(v*SX))
def Y(v): return int(round(v*SY))

fLarge = pygame.font.SysFont('dejavusansmono', int(38*FS), bold=True)
fMed   = pygame.font.SysFont('dejavusansmono', int(26*FS), bold=True)
fSmall = pygame.font.SysFont('dejavusansmono', int(18*FS))
fTiny  = pygame.font.SysFont('dejavusansmono', int(14*FS))

# ── SEVEN-SEGMENT ───────────────────────────────────────────────
SEG_MAP = {
    '0':'abcdef','1':'bc','2':'abdeg','3':'abcdg','4':'bcfg',
    '5':'acdfg', '6':'acdefg','7':'abc','8':'abcdefg','9':'abcdfg',
    '-':'g',' ':'','+':'bfg',
}
SEG_ON  = WHITE
SEG_OFF = (35, 30, 20)

def draw_seg(surf, ch, x, y, w, h, on=None, off=None):
    on  = on  or SEG_ON
    off = off or SEG_OFF
    segs = SEG_MAP.get(str(ch).upper(),'')
    t = max(3, int(min(w,h//2)*0.16))
    half = h//2
    rects = {
        'a': pygame.Rect(x+t, y,           w-2*t, t),
        'g': pygame.Rect(x+t, y+half-t//2, w-2*t, t),
        'd': pygame.Rect(x+t, y+h-t,       w-2*t, t),
        'f': pygame.Rect(x,   y+t,          t, half-t),
        'b': pygame.Rect(x+w-t, y+t,        t, half-t),
        'e': pygame.Rect(x,   y+half+t-t//2,t, half-t),
        'c': pygame.Rect(x+w-t, y+half+t-t//2,t, half-t),
    }
    for nm,r in rects.items():
        c = on if nm in segs else off
        pygame.draw.rect(surf, c, r, border_radius=max(1,t//3))

def draw_segs(surf, text, x, y, dw, dh, sp=None, on=None, off=None):
    sp = sp if sp is not None else max(3,int(dw*0.15))
    cx = x
    for ch in str(text):
        draw_seg(surf, ch, cx, y, dw, dh, on, off)
        cx += dw + sp
    return cx

# ── FLAME LOGIC ─────────────────────────────────────────────────
def flame_state(rpm, ticks):
    """Return (visible, color_top, color_mid, color_base, size_mult)"""
    if rpm < RPM_BLUE_START:
        return False, None, None, None, 0.0

    # Blink logic
    if RPM_BLINK_START <= rpm < RPM_RED_LINE:
        # blink rate: starts at ~2Hz at 5500, up to ~10Hz near redline
        t_range = RPM_RED_LINE - RPM_BLINK_START
        t_pos   = rpm - RPM_BLINK_START
        hz      = 2.0 + 8.0 * (t_pos / max(1, t_range))
        period  = 1000.0 / hz   # ms
        visible = (ticks % period) < (period * 0.55)
        if not visible:
            return False, None, None, None, 0.0

    t = min(1.0, (rpm - RPM_BLUE_START) / max(1, RPM_RED_LINE - RPM_BLUE_START))

    if rpm >= RPM_RED_LINE:
        # Solid red
        return True, (255,80,60), (255,30,10), (180,10,5), 1.15
    elif rpm >= RPM_GREEN_START:
        # Green → red transition
        g_t = (rpm - RPM_GREEN_START) / max(1, RPM_RED_LINE - RPM_GREEN_START)
        top  = _lerp_c((180,255,120), (255,100,40), g_t)
        mid  = _lerp_c((60, 230, 80), (255, 60, 10), g_t)
        base = _lerp_c((20, 160, 40), (200, 20,  5), g_t)
        sz   = 0.8 + 0.35 * g_t
        return True, top, mid, base, sz
    else:
        # Blue → green transition
        b_t = (rpm - RPM_BLUE_START) / max(1, RPM_GREEN_START - RPM_BLUE_START)
        top  = _lerp_c((120,160,255), (180,255,120), b_t)
        mid  = _lerp_c((40,  60,220), (60, 230, 80), b_t)
        base = _lerp_c((10,  20,160), (20, 160, 40), b_t)
        sz   = 0.45 + 0.35 * b_t
        return True, top, mid, base, sz

def _lerp_c(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

def draw_flame(surf, cx, cy, rpm, ticks):
    visible, c_top, c_mid, c_base, sz = flame_state(rpm, ticks)
    if not visible:
        # Draw dark dim ghost ball so position is clear
        pygame.draw.circle(surf, (20,18,14), (cx,cy), Y(90))
        pygame.draw.circle(surf, (35,30,20), (cx,cy), Y(90), width=2)
        lbl = fSmall.render('SHIFT', True, (50,45,35))
        surf.blit(lbl,(cx-lbl.get_width()//2, cy+Y(100)))
        return

    base_r = int(Y(85) * sz)
    pulse  = (math.sin(ticks/120.0)+1)/2   # slow outer pulse
    flick  = (math.sin(ticks/28.0) +1)/2   # fast inner flicker
    is_red = rpm >= RPM_RED_LINE

    # Outer glow layers
    n_glow = 6
    for gi in range(n_glow, 0, -1):
        glow_r   = int(base_r * (1.0 + 0.55*gi/n_glow*(0.7+0.3*pulse)))
        alpha    = int(30 * gi/n_glow * sz)
        glow_col = _lerp_c(c_base, c_mid, gi/n_glow)
        glow_surf = pygame.Surface((glow_r*2+4, glow_r*2+4), pygame.SRCALPHA)
        pygame.draw.circle(glow_surf, (*glow_col, alpha),
                           (glow_r+2, glow_r+2), glow_r)
        surf.blit(glow_surf, (cx-glow_r-2, cy-glow_r-2))

    # Main ball — radial gradient via concentric circles
    steps = 22
    for i in range(steps, 0, -1):
        frac  = i/steps
        r     = int(base_r * frac)
        if frac > 0.65:
            col = _lerp_c(c_base, c_mid, (frac-0.65)/0.35)
        else:
            col = _lerp_c(c_top, c_base, frac/0.65)
        pygame.draw.circle(surf, col, (cx,cy), r)

    # Bright core
    core_r = max(4, int(base_r*0.28*(0.85+0.15*flick)))
    core_c = _lerp_c(WHITE, c_top, 0.35+0.35*flick)
    pygame.draw.circle(surf, core_c, (cx,cy), core_r)

    # Lightning sparks when blinking/redline
    if rpm >= RPM_BLINK_START:
        fire_t  = min(1.0,(rpm-RPM_BLINK_START)/max(1,RPM_RED_LINE-RPM_BLINK_START))
        n_sparks = int(5*fire_t*(0.4+0.6*flick))
        for _ in range(n_sparks):
            ang   = random.uniform(0, math.pi*2)
            sp_r  = random.randint(base_r-Y(6), base_r+Y(18))
            sx    = int(cx + sp_r*math.cos(ang))
            sy    = int(cy + sp_r*math.sin(ang))
            sc    = _lerp_c(c_top, WHITE, random.random()*0.6)
            pygame.draw.circle(surf, sc, (sx,sy), random.randint(1,3))
        # Bolts
        n_bolts = int(3*fire_t*flick)
        for _ in range(n_bolts):
            bang  = random.uniform(0, math.pi*2)
            bx0   = int(cx + base_r*math.cos(bang))
            by0   = int(cy + base_r*math.sin(bang))
            bl    = random.randint(Y(10), Y(28))
            bsegs = random.randint(3,6)
            pts   = [(bx0,by0)]
            bxc,byc = bx0,by0
            for _ in range(bsegs):
                step = bl/bsegs
                jit  = random.randint(-Y(8),Y(8))
                bxc  = int(bxc+step*math.cos(bang))
                byc  = int(byc+step*math.sin(bang)+jit)
                pts.append((bxc,byc))
            if len(pts)>1:
                pygame.draw.lines(surf, WHITE, False, pts, 2)

    # Redline flash ring
    if is_red:
        pulse2 = (math.sin(ticks/60.0)+1)/2
        ring_r = int(base_r*(1.12+0.1*pulse2))
        ring_c = _lerp_c(RED_FLAME,(255,220,40),0.4*pulse2)
        pygame.draw.circle(surf, ring_c, (cx,cy), ring_r, width=max(2,Y(4)))

    # Label below flame
    lbl_txt = 'REDLINE!' if is_red else ('SHIFT NOW' if rpm>=RPM_GREEN_START else 'SHIFT')
    lbl_col = RED_FLAME if is_red else (GREEN_LT if rpm>=RPM_GREEN_START else BLUE_LT)
    lbl = fSmall.render(lbl_txt, True, lbl_col)
    surf.blit(lbl,(cx-lbl.get_width()//2, cy+base_r+Y(12)))

# ── DATA TABLE ──────────────────────────────────────────────────
TABLE_X = X(16)
TABLE_Y = SCREEN_H - Y(150)
TABLE_W = X(270)
TABLE_H = Y(140)
COL_LBL = X(120)
COL_VAL = TABLE_W - COL_LBL - X(8)

def draw_table(surf, data):
    r = pygame.Rect(TABLE_X, TABLE_Y, TABLE_W, TABLE_H)
    pygame.draw.rect(surf, PANEL, r, border_radius=6)
    pygame.draw.rect(surf, AMBER_DIM, r, width=1, border_radius=6)

    # Corner brackets
    bl = X(10)
    for pts in [
        [(r.left,r.top+bl),(r.left,r.top),(r.left+bl,r.top)],
        [(r.right-bl,r.top),(r.right,r.top),(r.right,r.top+bl)],
        [(r.left,r.bottom-bl),(r.left,r.bottom),(r.left+bl,r.bottom)],
        [(r.right-bl,r.bottom),(r.right,r.bottom),(r.right,r.bottom-bl)],
    ]:
        pygame.draw.lines(surf, AMBER, False, pts, 2)

    # Title
    title = fTiny.render('ENGINE DATA', True, AMBER_DIM)
    surf.blit(title,(r.x+X(8), r.y+Y(5)))
    pygame.draw.line(surf, AMBER_DIM,
                     (r.x+X(8), r.y+Y(20)), (r.right-X(8), r.y+Y(20)), 1)

    rows = [
        ('INTAKE AIR', data.get('intake'),  'C', lambda v: f'{v*9/5+32:.0f}F'),
        ('TIMING',     data.get('timing'),  '°', lambda v: f'{v:+.1f}°'),
        ('COOLANT',    data.get('coolant'), 'C', lambda v: f'{v*9/5+32:.0f}F'),
    ]
    row_h = Y(34)
    for i,(lbl,val,unit,fmt) in enumerate(rows):
        ry  = r.y + Y(26) + i*row_h
        has = val is not None
        lc  = AMBER if has else AMBER_DIM
        lt  = fSmall.render(lbl, True, lc)
        surf.blit(lt,(r.x+X(10), ry+row_h//2-lt.get_height()//2))
        vstr = fmt(val) if has else '---'
        vt   = fMed.render(vstr, True, AMBER if has else DIM)
        surf.blit(vt,(r.x+COL_LBL, ry+row_h//2-vt.get_height()//2))

# ── RPM READOUT ─────────────────────────────────────────────────
RPM_DIG_X = X(16)
RPM_DIG_Y = SCREEN_H - Y(170) - Y(60)
RPM_DW    = X(28); RPM_DH = Y(46); RPM_SP = X(5)

def draw_rpm_readout(surf, rpm, ticks):
    is_lim = rpm >= RPM_RED_LINE
    pulse  = (math.sin(ticks/90.0)+1)/2
    col    = rpm_col(rpm)
    if is_lim:
        col = _lerp_c(RED_FLAME,(255,200,220),pulse*0.5)
    # Ghost
    draw_segs(surf,'8888',RPM_DIG_X,RPM_DIG_Y,RPM_DW,RPM_DH,RPM_SP,SEG_OFF,SEG_OFF)
    # Live
    draw_segs(surf,f'{int(rpm):04d}',RPM_DIG_X,RPM_DIG_Y,RPM_DW,RPM_DH,RPM_SP,col,PANEL)
    lbl = fTiny.render('RPM', True, AMBER_DIM)
    surf.blit(lbl,(RPM_DIG_X, RPM_DIG_Y+RPM_DH+Y(3)))

def rpm_col(rpm):
    if rpm < 3000:  return AMBER
    if rpm < 5000:  return _lerp_c(BLUE_LT, GREEN_LT,(rpm-3000)/2000)
    if rpm < 6600:  return _lerp_c(GREEN_LT, RED_FLAME,(rpm-5000)/1600)
    return RED_FLAME

# ── MAIN ────────────────────────────────────────────────────────
def main():
    conn   = obd.OBD()
    poller = Poller(conn)
    poller.start()

    disp_rpm = 0.0
    running  = True

    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False

        data    = poller.snapshot()
        raw_rpm = data.get('rpm') or 0

        # Smooth RPM
        dt       = max(0.001, clock.get_time()/1000.0)
        alpha    = min(1.0, 6.0*dt)
        disp_rpm += (raw_rpm - disp_rpm)*alpha
        if abs(disp_rpm-raw_rpm) < 8: disp_rpm = float(raw_rpm)

        ticks = pygame.time.get_ticks()

        # ── DRAW ──────────────────────────────────────────────
        screen.fill(BLACK)

        # Subtle grid lines — very dark
        for gx in range(0, SCREEN_W, X(80)):
            pygame.draw.line(screen,(16,14,10),(gx,0),(gx,SCREEN_H),1)
        for gy in range(0, SCREEN_H, Y(80)):
            pygame.draw.line(screen,(16,14,10),(0,gy),(SCREEN_W,gy),1)

        # Flame — centered right side
        flame_cx = int(SCREEN_W * 0.65)
        flame_cy = int(SCREEN_H * 0.44)
        draw_flame(screen, flame_cx, flame_cy, disp_rpm, ticks)

        # RPM readout — above table, bottom left
        draw_rpm_readout(screen, disp_rpm, ticks)

        # Data table — bottom left
        draw_table(screen, data)

        # Thin separator line between left info and right flame
        sep_x = X(310)
        pygame.draw.line(screen, (30,26,18),(sep_x,Y(20)),(sep_x,SCREEN_H-Y(20)),1)

        # Next slow poll countdown
        since = time_mod.monotonic() - poller._last_slow
        nxt   = max(0.0, SLOW_INTERVAL - since)
        ct    = fTiny.render(f'DATA REFRESH {nxt:.0f}s', True, (35,30,20))
        screen.blit(ct,(TABLE_X, TABLE_Y - Y(18)))

        pygame.display.flip()
        clock.tick(30)

    poller.stop()
    pygame.quit()
    conn.close()
    sys.exit()

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt:
        pygame.quit(); sys.exit()

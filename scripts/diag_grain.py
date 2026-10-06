import sys, glob, os
sys.path.insert(0, r'D:/IO_paint')
import numpy as np, cv2
from app.engine import InpaintEngine
from app.grain import _local_sigma, estimate_grain, MASK_THRESHOLD

def imread_u(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)

e = InpaintEngine()
imgs = sorted(glob.glob(r'D:/IO_paint/samples/pick-test/*.jpg'))
print(f'{"file":<24} {"cur":>9} {"surround":>9} {"ratio":>7}  verdict')
print('-'*68)
dead = tot = 0
for p in imgs:
    bgr = imread_u(p)
    if bgr is None: print('skip', p); continue
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    for (fy, fx) in [(0.35, 0.35), (0.15, 0.55)]:
        mask = np.zeros((h, w), np.uint8)
        y0, y1 = int(h*fy), min(h, int(h*fy)+int(h*0.3))
        x0, x1 = int(w*fx), min(w, int(w*fx)+int(w*0.3))
        mask[y0:y1, x0:x1] = 255
        if mask.max() == 0: continue
        sel = mask > MASK_THRESHOLD
        out, _ = e.inpaint(rgb, mask, grain=0.0)
        cur = float(np.median(_local_sigma(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY),5)[sel]))
        tgt = estimate_grain(rgb, mask)['value']
        ratio = tgt / max(cur, 1e-9); tot += 1
        v = 'DEAD' if ratio <= 1.02 else 'ok'
        if ratio <= 1.02: dead += 1
        print(f'{os.path.basename(p)[:22]:<24} {cur:>9.5f} {tgt:>9.5f} {ratio:>7.3f}  {v}')
print('-'*68)
print(f'total {tot} spots -> {dead} in DEAD zone (0~1.0 no-op)')

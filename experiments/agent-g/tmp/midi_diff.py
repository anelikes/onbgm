import mido, sys
from collections import defaultdict
def load(p):
    m = mido.MidiFile(p)
    res = {}
    for i, tr in enumerate(m.tracks):
        name = tr.name or f"track{i}"
        t = 0; tempo_map = []
        notes = []; on = {}
        abs_t = 0
        for msg in tr:
            abs_t += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                on[(msg.channel, msg.note)] = (abs_t, msg.velocity)
            elif msg.type in ('note_off', 'note_on'):
                k = (msg.channel, msg.note)
                if k in on:
                    st, v = on.pop(k); notes.append((st, abs_t, msg.note, v))
        cc = [(abs_t2, msg.control, msg.value) for abs_t2, msg in []]
        res[name] = sorted(notes)
    return m, res
m1, a = load(sys.argv[1]); m2, b = load(sys.argv[2])
print("tpb", m1.ticks_per_beat, m2.ticks_per_beat)
# convert ticks to seconds using merged tempo map of file 1
def tick2sec(m):
    tempos = []
    for tr in m.tracks:
        t = 0
        for msg in tr:
            t += msg.time
            if msg.type == 'set_tempo': tempos.append((t, msg.tempo))
    tempos.sort()
    def f(tick):
        sec = 0; last_t = 0; cur = 500000
        for tt, tp in tempos:
            if tt >= tick: break
            sec += (tt - last_t) * cur / 1e6 / m.ticks_per_beat; last_t = tt; cur = tp
        return sec + (tick - last_t) * cur / 1e6 / m.ticks_per_beat
    return f
f1 = tick2sec(m1); f2 = tick2sec(m2)
lo, hi = float(sys.argv[3]), float(sys.argv[4])
for name in sorted(set(a) | set(b)):
    na = [(round(f1(s),3), round(f1(e),3), n, v) for s,e,n,v in a.get(name, [])]
    nb = [(round(f2(s),3), round(f2(e),3), n, v) for s,e,n,v in b.get(name, [])]
    oa = [x for x in na if not (lo - 0.001 <= x[0] < hi - 0.001)]
    ob = [x for x in nb if not (lo - 0.001 <= x[0] < hi - 0.001)]
    ia = [x for x in na if (lo - 0.001 <= x[0] < hi - 0.001)]
    ib = [x for x in nb if (lo - 0.001 <= x[0] < hi - 0.001)]
    print(f"{name:12s} outside: v1={len(oa)} v2={len(ob)} identical={oa==ob}  inside: v1={len(ia)} v2={len(ib)} identical={ia==ib}")
    if oa != ob:
        sa, sb = set(oa), set(ob)
        print("   only v1:", sorted(sa - sb)[:6]); print("   only v2:", sorted(sb - sa)[:6])

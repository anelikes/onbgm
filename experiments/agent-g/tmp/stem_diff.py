import sys, numpy as np
try:
    import soundfile as sf
    read = lambda p: sf.read(p)
except ImportError:
    from scipy.io import wavfile
    def read(p):
        sr, x = wavfile.read(p); return x.astype(np.float64), sr
d1, d2 = sys.argv[1], sys.argv[2]
regions = {"intro":(0,5.3),"person":(5.5,16.8),"montage..cut3":(16.9,28.3),"slowmo":(28.6,30.5),"ending+tail":(30.7,38.0)}
for stem in sys.argv[3:]:
    x1, sr = read(f"{d1}/{stem}.wav"); x2, _ = read(f"{d2}/{stem}.wav")
    out = []
    for name,(a,b) in regions.items():
        s1 = x1[int(a*sr):int(b*sr)]; s2 = x2[int(a*sr):int(b*sr)]
        r1 = np.sqrt(np.mean(s1**2)) + 1e-12; r2 = np.sqrt(np.mean(s2**2)) + 1e-12
        out.append(f"{name}:{20*np.log10(r2/r1):+.2f}dB")
    print(f"{stem:10s}", "  ".join(out))

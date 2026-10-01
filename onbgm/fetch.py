"""下载音源到 sounds/。

- GeneralUser GS：通用 GM 音源，所有音色的保底实现
- Salamander Grand Piano（CC-BY 3.0）：三角钢琴
- VSCO 2 Community Edition（CC0）：弦乐分部、低音提琴、定音鼓、镲；只下载用得到的部分
"""
from __future__ import annotations

import json
import lzma
import shutil
import tarfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GENERALUSER_URL = "https://github.com/mrbumpy409/GeneralUser-GS/raw/main/GeneralUser-GS.sf2"
SALAMANDER_URL = "https://freepats.zenvoid.org/Piano/SalamanderGrandPiano/SalamanderGrandPiano-SF2-V3+20200602.tar.xz"
VSCO_REPO = "sgossner/VSCO-2-CE"
VSCO_PARTS = [
    "Strings/Violin Section/susVib/",
    "Strings/Viola Section/susvib/",
    "Strings/Cello Section/susvib/",
    "Strings/Cello Section/pizzT/",
    "Strings/Solo Contrabass/SusVib/",
    "Strings/Solo Contrabass/Pizz/",
    "Strings/Violin Section/Spic/",
    "Strings/Viola Section/spic/",
    "Strings/Cello Section/spic/",
    "Strings/Solo Contrabass/Spic/",
    "Percussion/Timpani/",
    "Percussion/cymbal-crash1_",
    "Percussion/Glock/",
]


def _download(url: str, dest: Path, parallel: int = 16) -> None:
    """大文件按字节范围分段并行下载（有的镜像限制单连接速度），每段单独存盘，断了可以续传。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "onbgm"})
    with urllib.request.urlopen(head, timeout=60) as r:
        size = int(r.headers.get("Content-Length") or 0)
        ranged = r.headers.get("Accept-Ranges") == "bytes"
    if not ranged or size < 50_000_000:
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "onbgm"}), timeout=60) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        tmp.rename(dest)
        return

    step = -(-size // parallel)
    pieces = [(i, i * step, min(size, (i + 1) * step)) for i in range(parallel)]

    def piece_path(i: int) -> Path:
        return dest.with_suffix(dest.suffix + f".part{i:02d}")

    def fetch_piece(piece) -> None:
        i, a, b = piece
        path = piece_path(i)
        for attempt in range(6):
            have = path.stat().st_size if path.exists() else 0
            if have >= b - a:
                return
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "onbgm", "Range": f"bytes={a + have}-{b - 1}"})
                with urllib.request.urlopen(req, timeout=30) as r, open(path, "ab") as f:
                    shutil.copyfileobj(r, f, length=1 << 16)
            except OSError:
                if attempt == 5:
                    raise

    with ThreadPoolExecutor(parallel) as pool:
        list(pool.map(fetch_piece, pieces))
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as out:
        for i, _, _ in pieces:
            with open(piece_path(i), "rb") as f:
                shutil.copyfileobj(f, out, length=1 << 20)
    if tmp.stat().st_size != size:
        raise OSError(f"下载不完整：{tmp.stat().st_size} / {size} 字节")
    tmp.rename(dest)
    for i, _, _ in pieces:
        piece_path(i).unlink()


def fetch_generaluser(root: Path) -> None:
    dest = root / "GeneralUser-GS.sf2"
    if not dest.exists():
        print("下载 GeneralUser GS ...")
        _download(GENERALUSER_URL, dest)


def fetch_salamander(root: Path) -> None:
    dest = root / "SalamanderGrandPiano.sf2"
    if dest.exists():
        return
    archive = root / "salamander.tar.xz"
    if not archive.exists():
        print("下载 Salamander Grand Piano（约 310MB）...")
        _download(SALAMANDER_URL, archive)
    print("解压 Salamander ...")
    with tarfile.open(fileobj=lzma.open(archive)) as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith(".sf2"))
        with tar.extractfile(member) as src, open(dest.with_suffix(".part"), "wb") as out:
            shutil.copyfileobj(src, out, length=1 << 20)
    dest.with_suffix(".part").rename(dest)
    archive.unlink()


def fetch_vsco(root: Path) -> None:
    api = f"https://api.github.com/repos/{VSCO_REPO}/git/trees/master?recursive=1"
    with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "onbgm"})) as r:
        tree = json.load(r)["tree"]
    files = [x for x in tree if x["type"] == "blob" and x["path"].lower().endswith(".wav")
             and any(x["path"].startswith(p) for p in VSCO_PARTS)]
    todo = [x for x in files if not (root / "vsco2" / x["path"]).exists()
            or (root / "vsco2" / x["path"]).stat().st_size != x["size"]]
    print(f"VSCO 2 CE: {len(files)} 个采样，需下载 {len(todo)} 个（{sum(x['size'] for x in todo) / 1e6:.0f}MB）...")

    def get(x):
        url = f"https://raw.githubusercontent.com/{VSCO_REPO}/master/{urllib.parse.quote(x['path'])}"
        _download(url, root / "vsco2" / x["path"])

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(get, todo))
    missing = [x["path"] for x in files if not (root / "vsco2" / x["path"]).exists()
               or (root / "vsco2" / x["path"]).stat().st_size != x["size"]]
    if missing:
        raise OSError(f"VSCO 有 {len(missing)} 个采样没下载完整（例如 {missing[0]}），请重新运行 onbgm fetch")


def fetch_all(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fetch_generaluser(root)
    fetch_vsco(root)
    fetch_salamander(root)
    print("音源就绪:", root)

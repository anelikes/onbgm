"""下载音源到 sounds/。

- GeneralUser GS：通用 GM 音源，所有音色的保底实现
- Salamander Grand Piano（CC-BY 3.0）：三角钢琴
- VSCO 2 Community Edition（CC0）：弦乐分部、低音提琴、定音鼓、镲；只下载用得到的部分
- SFZ 采样库（见 SFZ_LIBS）：电钢琴、木吉他、电贝斯、原声鼓组。GitHub 上的固定到具体提交，作者更新不会改变我们的声音
"""
from __future__ import annotations

import json
import lzma
import re
import shutil
import tarfile
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SfzLib:
    name: str  # 存放在 sounds/sfz/<name>/
    title: str
    author: str
    license: str
    url: str  # 项目主页
    used_for: str
    program: str  # SFZ 文件在目录里的相对路径
    archive: str | None = None  # 来源一：zip 或 tar.xz 压缩包，整个解压
    repo: str | None = None  # 来源二：GitHub 仓库里的部分文件
    sha: str | None = None
    select: str = ""  # 要下载的文件路径（正则）
    strip: str = ""  # 存盘时去掉的路径前缀


SFZ_LIBS = [
    SfzLib("jrhodes", "jRhodes（GM 版）", "Jeff Learman", "CC0", "https://github.com/sfzinstruments/Discord-SFZ-GM-Bank",
           "电钢琴（1977 年 Rhodes Mark I）", "005-Electric Piano 1.sfz",
           repo="sfzinstruments/Discord-SFZ-GM-Bank", sha="7a9c478fe331f94f246d33332f0adedb25bbbe27",
           select=r"Discord GM/Melodic/005-Electric Piano 1(\.sfz|/A_.*\.wav)$", strip="Discord GM/Melodic/"),
    SfzLib("fss-guitar", "FSS Steel String Guitar", "Gary Campion / FlameStudios，FreePats 整理",
           "GPL-3.0+（附例外：用它做的音乐不受 GPL 约束）",
           "https://freepats.zenvoid.org/Guitar/steel-acoustic-guitar.html", "钢弦木吉他",
           "FSS-SteelStringGuitar-SFZ-20200521/FSS-SteelStringGuitar-20200521.sfz",
           archive="https://freepats.zenvoid.org/Guitar/FSS-SteelStringGuitar/FSS-SteelStringGuitar-SFZ-20200521.tar.xz"),
    SfzLib("black-and-blue", "Black And Blue Basses（Darkblack 指弹）", "Karoryfer Samples", "CC0",
           "https://github.com/sfzinstruments/karoryfer.black-and-blue-basses", "电贝斯（五弦，指弹）",
           "Programs/05-darkblack_pluck.sfz",
           repo="sfzinstruments/karoryfer.black-and-blue-basses", sha="6e7d674cdb41be7a54dbccb15472401ad01099b9",
           select=r"(Programs/.*\.sfz|Samples/darkblack/reg/.*_rr[12]\.wav)$"),  # 只取 4 个轮替里的 2 个，省一半体积
    SfzLib("black-pearl", "AVL Drumkits: Black Pearl", "Glen MacArthur", "CC-BY-SA 3.0（用它做的音乐不需要署名）",
           "https://www.bandshed.net/avldrumkits/", "标准鼓组（Pearl 22 寸底鼓）",
           "BLACK_PEARL_2023_repack/Black_Pearl_2023_repack.sfz",
           archive="https://www.bandshed.net/sounds/AVLDrumkits_SFZ/BLACK_PEARL_2023_repack.zip"),
    SfzLib("blonde-bop", "AVL Drumkits: Blonde Bop", "Glen MacArthur", "CC-BY-SA 3.0（用它做的音乐不需要署名）",
           "https://www.bandshed.net/avldrumkits/", "爵士鼓组（TAMA 18 寸底鼓）",
           "BLONDE_BOP_SFZ/BLONDE_BOP.sfz",
           archive="https://www.bandshed.net/sounds/AVLDrumkits_SFZ/BLONDE_BOP_SFZ.zip"),
]


def sfz_path(lib: SfzLib, root: Path) -> Path:
    return root / "sfz" / lib.name / lib.program


def fetch_sfz(lib: SfzLib, root: Path) -> None:
    dest = root / "sfz" / lib.name
    if lib.archive:
        if sfz_path(lib, root).exists():
            return
        archive = dest.parent / Path(urllib.parse.urlparse(lib.archive).path).name
        print(f"下载 {lib.title} ...")
        _download(lib.archive, archive)
        dest.mkdir(parents=True, exist_ok=True)
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
        else:
            with tarfile.open(fileobj=lzma.open(archive)) as tar:
                tar.extractall(dest, filter="data")
        archive.unlink()
        return
    api = f"https://api.github.com/repos/{lib.repo}/git/trees/{lib.sha}?recursive=1"
    with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": "onbgm"})) as r:
        tree = json.load(r)["tree"]
    files = [x for x in tree if x["type"] == "blob" and re.match(lib.select, x["path"])]

    def local(x) -> Path:
        return dest / x["path"].removeprefix(lib.strip)

    todo = [x for x in files if not local(x).exists() or local(x).stat().st_size != x["size"]]
    if not todo:
        return
    print(f"{lib.title}: {len(files)} 个文件，需下载 {len(todo)} 个（{sum(x['size'] for x in todo) / 1e6:.0f}MB）...")

    def get(x):
        url = f"https://raw.githubusercontent.com/{lib.repo}/{lib.sha}/{urllib.parse.quote(x['path'])}"
        _download(url, local(x))

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(get, todo))
    missing = [x["path"] for x in files if not local(x).exists() or local(x).stat().st_size != x["size"]]
    if missing:
        raise OSError(f"{lib.title} 有 {len(missing)} 个文件没下载完整（例如 {missing[0]}），请重新运行 onbgm fetch")


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
    for lib in SFZ_LIBS:
        fetch_sfz(lib, root)
    print("音源就绪:", root)

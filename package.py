"""把 dist/PeriphBattery.exe、说明书、AI 提示词和 device 配置打成一个便携包。

用法：
    python package.py                       # 生成 dist/PeriphBattery-便携版.zip
    python package.py 输出名.zip             # 指定输出文件名（CI 用 ASCII 名）
"""

import glob
import os
import sys
import zipfile

# CI（英文 Windows）控制台默认不是 UTF-8，直接 print 中文会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
INNER = "外设电量监视"          # 压缩包内的文件夹名

# 放在包根目录的文件
TOP_FILES = [
    "dist/PeriphBattery.exe",
    "使用说明.txt",
    "AI提示词-生成设备配置.md",
]


def collect() -> list:
    """返回 [(绝对路径, 压缩包内名字)]。"""
    items = []
    for rel in TOP_FILES:
        items.append((os.path.join(ROOT, rel), os.path.basename(rel)))
    for path in sorted(glob.glob(os.path.join(ROOT, "device", "*.json"))):
        items.append((path, "device/" + os.path.basename(path)))
    return items


def main() -> int:
    out_name = sys.argv[1] if len(sys.argv) > 1 else "PeriphBattery-便携版.zip"
    out = os.path.join(DIST, out_name)

    items = collect()
    missing = [p for p, _ in items if not os.path.isfile(p)]
    # device 目录为空时单独提示（glob 不会产生缺失项）
    if not any(n.startswith("device/") for _, n in items):
        print("device 目录里没有 json 配置，先放至少一个配置文件。")
        return 1
    if missing:
        print("缺少文件，先执行打包：")
        for m in missing:
            print("  -", os.path.relpath(m, ROOT))
        for d in ("dist", "device"):
            print("[目录] %s: %s" % (d, sorted(os.listdir(os.path.join(ROOT, d)))))
        return 1

    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full, name in items:
            z.write(full, INNER + "/" + name)

    print("便携包: %s  %.2f MB（%d 个文件）" % (out, os.path.getsize(out) / 1048576, len(items)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

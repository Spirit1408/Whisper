"""Build the offline Windows D4 probe from the installed CPython 3.11 and project wheels."""
from pathlib import Path
import shutil
import subprocess
import sys
import os

ROOT = Path(__file__).resolve().parents[2]
QT_FILES = ("__init__.py", "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "pyside6.abi3.dll",
            "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "plugins/platforms/qwindows.dll")
PACKAGES = """_cffi_backend.cp311-win_amd64.pyd _sounddevice_data _sounddevice.py sounddevice.py
cffi pycparser shiboken6 av av.libs ctranslate2 numpy numpy.libs faster_whisper onnxruntime
tokenizers huggingface_hub tqdm yaml packaging fsspec filelock requests urllib3 certifi
charset_normalizer idna httpx httpcore anyio h11 typing_extensions.py colorama
flatbuffers google six.py""".split()

HOST = r"""
using System;
using System.IO;
using System.Runtime.InteropServices;
class Probe {
    [DllImport("python311.dll", CallingConvention=CallingConvention.Cdecl)]
    static extern int Py_Main(int argc, IntPtr argv);
    static int Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        Environment.SetEnvironmentVariable("PYTHONHOME", root);
        Environment.SetEnvironmentVariable("PYTHONPATH", null);
        Environment.SetEnvironmentVariable("PATH", root + ";" + Environment.SystemDirectory);
        string[] opts = {Path.Combine(root, "Probe.exe"), "-s", "-S", "-B", Path.Combine(root, "probe.py")};
        string[] all = new string[opts.Length + args.Length];
        opts.CopyTo(all, 0);
        args.CopyTo(all, opts.Length);
        IntPtr[] strings = Array.ConvertAll(all, Marshal.StringToHGlobalUni);
        IntPtr argv = Marshal.AllocHGlobal(IntPtr.Size * strings.Length);
        Marshal.Copy(strings, 0, argv, strings.Length);
        try { return Py_Main(strings.Length, argv); }
        finally {
            foreach (var s in strings) Marshal.FreeHGlobal(s);
            Marshal.FreeHGlobal(argv);
        }
    }
}
"""


def copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns(
            "site-packages", "__pycache__", "tests", "test", "idlelib", "tkinter",
            "ensurepip", "include", "*.lib", "*.pdb"))
    else:
        shutil.copy2(source, dest)


def build(destination):
    if sys.version_info[:2] != (3, 11) or os.name != "nt":
        raise SystemExit("Requires Windows x64 CPython 3.11")
    destination.mkdir(parents=True, exist_ok=False)
    home = Path(sys.base_prefix)
    for item in ("Lib", "DLLs", "python311.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        copy(home / item, destination / item)
    site = ROOT / ".venv/Lib/site-packages"
    for item in [*PACKAGES, "nvidia/cublas/bin", "nvidia/cudnn/bin"]:
        copy(site / item, destination / "packages" / item)
    for item in QT_FILES:
        copy(site / "PySide6" / item, destination / "packages/PySide6" / item)
    # Some library features (including hub import) read wheel metadata at runtime.
    for dist in site.glob("*.dist-info"):
        if (dist / "METADATA").is_file():
            copy(dist / "METADATA", destination / "packages" / dist.name / "METADATA")
    copy(ROOT / "src/transcriber.py", destination / "transcriber.py")
    copy(Path(__file__).with_name("bundle_probe.py"), destination / "probe.py")
    (destination / "Probe.cs").write_text(HOST, encoding="utf-8")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([compiler, "/nologo", "/platform:x64", "/out:Probe.exe", "Probe.cs"],
                   cwd=destination, check=True)
    print("Built local D4 probe")


if __name__ == "__main__":
    build(Path(sys.argv[1]).resolve())

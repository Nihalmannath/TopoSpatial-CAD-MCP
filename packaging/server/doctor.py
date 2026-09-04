"""
TopoSpatial-CAD Pre-flight Health & Diagnostic Engine
Validates the entire toolchain:
Claude Desktop -> MCP Server -> TopoSpatial Logic -> CAD Bridge -> AutoCAD

Verifies:
✓ Python runtime (>= 3.10)
✓ Windows platform (win32)
✓ pywin32 & COM subsystem
✓ AutoCAD installation & registry keys
✓ AutoCAD running process (acad.exe)
✓ AutoCAD COM automation interface
✓ Active drawing document availability
✓ Spatial topology runtime (TopologicPy / Shapely fallback)
"""

import sys
import os
import platform
from typing import Dict, List, Any


def check_python_runtime() -> Dict[str, Any]:
    ver = sys.version_info
    passes = (ver.major == 3 and ver.minor >= 10)
    return {
        "title": "Python Runtime",
        "passed": passes,
        "detail": f"{platform.python_version()} ({'64-bit' if sys.maxsize > 2**32 else '32-bit'})",
        "remediation": "Please install Python 3.10 or newer (64-bit recommended)." if not passes else None
    }


def check_windows_platform() -> Dict[str, Any]:
    is_win = sys.platform == "win32"
    return {
        "title": "Operating System",
        "passed": is_win,
        "detail": f"{platform.system()} {platform.release()} ({platform.version()})",
        "remediation": "AutoCAD COM automation is exclusive to Microsoft Windows (Windows 10/11)." if not is_win else None
    }


def check_pywin32_subsystem() -> Dict[str, Any]:
    try:
        import win32com.client  # noqa: F401
        import pythoncom  # noqa: F401
        return {
            "title": "Windows COM Bridge (pywin32)",
            "passed": True,
            "detail": "pywin32 & pythoncom available",
            "remediation": None
        }
    except ImportError:
        return {
            "title": "Windows COM Bridge (pywin32)",
            "passed": False,
            "detail": "pywin32 module not found",
            "remediation": "Run 'pip install pywin32' to enable Windows COM automation."
        }


def check_autocad_installed() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"title": "AutoCAD Installation", "passed": False, "detail": "Non-Windows OS", "remediation": None}
    
    try:
        import winreg
        installed_versions = []
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(root, r"SOFTWARE\Autodesk\AutoCAD")
                num_subkeys, _, _ = winreg.QueryInfoKey(key)
                for i in range(num_subkeys):
                    sub = winreg.EnumKey(key, i)
                    installed_versions.append(sub)
                winreg.CloseKey(key)
            except Exception:
                pass

        if installed_versions:
            return {
                "title": "AutoCAD Installation",
                "passed": True,
                "detail": f"Detected registry versions: {', '.join(sorted(set(installed_versions)))}",
                "remediation": None
            }
        else:
            return {
                "title": "AutoCAD Installation",
                "passed": True,  # Non-blocking, might be installed under custom path
                "detail": "No default registry key found; COM will probe dynamically",
                "remediation": None
            }
    except Exception as exc:
        return {
            "title": "AutoCAD Installation",
            "passed": True,
            "detail": f"Registry check skipped: {exc}",
            "remediation": None
        }


def check_autocad_process() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"title": "CAD Running Process", "passed": False, "detail": "Non-Windows OS", "remediation": None}

    running_cads = []
    try:
        import subprocess
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5)
        output = result.stdout.lower()
        if "acad.exe" in output:
            running_cads.append("AutoCAD (acad.exe)")
        if "zwcad.exe" in output:
            running_cads.append("ZWCAD (zwcad.exe)")
        if "gcad.exe" in output:
            running_cads.append("GstarCAD (gcad.exe)")
        if "bricscad.exe" in output:
            running_cads.append("BricsCAD (bricscad.exe)")
    except Exception:
        pass

    if running_cads:
        return {
            "title": "CAD Process Running",
            "passed": True,
            "detail": ", ".join(running_cads),
            "remediation": None
        }
    return {
        "title": "CAD Process Running",
        "passed": False,
        "detail": "No active CAD process detected in tasklist",
        "remediation": "Start AutoCAD, ZWCAD, or GstarCAD before running design commands."
    }


def check_cad_com_connection() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"title": "CAD COM Connection", "passed": False, "detail": "COM unsupported on non-Windows", "remediation": None}

    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()
        cad = None
        prog_ids = [
            "AutoCAD.Application",
            "AutoCAD.Application.25",  # 2025
            "AutoCAD.Application.24",  # 2024
            "ZWCAD.Application",
            "GstarCAD.Application",
        ]
        connected_id = None
        for pid in prog_ids:
            try:
                cad = win32com.client.GetActiveObject(pid)
                connected_id = pid
                break
            except Exception:
                continue

        if cad:
            version_str = getattr(cad, "Version", "Unknown")
            name_str = getattr(cad, "Name", connected_id)
            return {
                "title": "CAD COM Connection",
                "passed": True,
                "detail": f"Connected to {name_str} (v{version_str}) via {connected_id}",
                "remediation": None,
                "cad_object": cad
            }
        else:
            return {
                "title": "CAD COM Connection",
                "passed": False,
                "detail": "Could not connect to active CAD COM interface",
                "remediation": "Launch AutoCAD and ensure it is not running in an elevated Administrator prompt while Claude runs standard."
            }
    except Exception as exc:
        return {
            "title": "CAD COM Connection",
            "passed": False,
            "detail": f"COM error: {exc}",
            "remediation": "Ensure pywin32 is installed and AutoCAD is running."
        }


def check_active_document(com_result: Dict[str, Any]) -> Dict[str, Any]:
    cad = com_result.get("cad_object")
    if not cad:
        return {
            "title": "Active Drawing Document",
            "passed": False,
            "detail": "Skipped (no active CAD connection)",
            "remediation": "Open a .dwg drawing inside AutoCAD."
        }

    try:
        doc = getattr(cad, "ActiveDocument", None)
        if doc and getattr(doc, "Name", None):
            return {
                "title": "Active Drawing Document",
                "passed": True,
                "detail": f"Active: '{doc.Name}' (Read-only: {getattr(doc, 'ReadOnly', False)})",
                "remediation": None
            }
        else:
            return {
                "title": "Active Drawing Document",
                "passed": False,
                "detail": "AutoCAD is open but no drawing document (.dwg) is active",
                "remediation": "Create a new drawing or open an existing drawing in AutoCAD."
            }
    except Exception as exc:
        return {
            "title": "Active Drawing Document",
            "passed": False,
            "detail": f"Document query error: {exc}",
            "remediation": "Click into AutoCAD to activate a drawing tab."
        }


def check_topology_engine() -> Dict[str, Any]:
    has_topologic = False
    has_shapely = False
    try:
        import topologicpy  # noqa: F401
        has_topologic = True
    except ImportError:
        pass

    try:
        import shapely  # noqa: F401
        import networkx  # noqa: F401
        has_shapely = True
    except ImportError:
        pass

    if has_topologic:
        return {
            "title": "Topology Engine",
            "passed": True,
            "detail": "TopologicPy C++ geometric kernel loaded",
            "remediation": None
        }
    elif has_shapely:
        return {
            "title": "Topology Engine",
            "passed": True,
            "detail": "Shapely & NetworkX topological spatial fallback ready",
            "remediation": None
        }
    else:
        return {
            "title": "Topology Engine",
            "passed": False,
            "detail": "Neither TopologicPy nor Shapely installed",
            "remediation": "Run 'pip install shapely networkx' for geometric computing."
        }


def run_full_diagnostics() -> Dict[str, Any]:
    """Run comprehensive diagnostics for the entire CAD MCP toolchain."""
    py_check = check_python_runtime()
    win_check = check_windows_platform()
    com_check = check_pywin32_subsystem()
    inst_check = check_autocad_installed()
    proc_check = check_autocad_process()
    conn_check = check_cad_com_connection()
    doc_check = check_active_document(conn_check)
    topo_check = check_topology_engine()

    # Clean out non-serializable cad_object
    conn_check_clean = {k: v for k, v in conn_check.items() if k != "cad_object"}

    checks = [
        py_check,
        win_check,
        com_check,
        inst_check,
        proc_check,
        conn_check_clean,
        doc_check,
        topo_check,
    ]

    all_ok = all(c["passed"] for c in checks)
    cad_ready = conn_check["passed"] and doc_check["passed"]

    remediations = [c["remediation"] for c in checks if not c["passed"] and c.get("remediation")]

    return {
        "healthy": all_ok,
        "cad_ready": cad_ready,
        "checks": checks,
        "remediations": remediations,
    }


def print_doctor_report():
    """Print an interactive formatted status report."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    use_unicode = sys.stdout.encoding and "utf" in sys.stdout.encoding.lower()
    ok_sym = "✓" if use_unicode else "OK"
    fail_sym = "X" if use_unicode else "FAIL"

    print("=" * 65)
    print("   TopoSpatial-CAD MCP System Pre-flight Doctor")
    print("=" * 65)
    
    diag = run_full_diagnostics()
    for c in diag["checks"]:
        mark = ok_sym if c["passed"] else fail_sym
        color = "\033[92m" if c["passed"] else "\033[91m"
        reset = "\033[0m"
        print(f"  [{color}{mark}{reset}] {c['title']:<30} : {c['detail']}")
        if not c["passed"] and c.get("remediation"):
            print(f"      -> Hint: {c['remediation']}")

    print("-" * 65)
    if diag["cad_ready"]:
        print("  Status: READY - CAD bridge and active document verified!")
    elif diag["checks"][0]["passed"] and diag["checks"][1]["passed"]:
        print("  Status: MCP READY (CAD IDLE) - Server can start; connect CAD when needed.")
    else:
        print("  Status: PREREQUISITES MISSING - Check items marked [✕] above.")
    print("=" * 65)


if __name__ == "__main__":
    print_doctor_report()

"""Unit test logika DIFF murni (tanpa SSH): _split_verb, _is_excluded,
_collapse_deletes, compute_diff, load_exclude_patterns.

Jalankan: python test_diff.py    (exit 0 = semua lolos)

Semua fungsi yang diuji bersifat MURNI (tak menyentuh router), jadi kita bisa
menembak semua kemungkinan: add, delete-runtuh, ganti-nilai, status
activate/deactivate, pengecualian scope all/state, dan kombinasinya.
"""
import importlib.util
import os
import sys
import tempfile

# Muat sync-juniper.py sebagai modul (nama file pakai '-', tak bisa import biasa).
sys.argv = ["sj"]
_spec = importlib.util.spec_from_file_location("sj", "sync-juniper.py")
sj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sj)

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       want: {want}")
        print(f"       got : {got}")
        _fails.append(name)


def L(s):
    """Bantu: multiline → list baris tak-kosong (meniru fetch_set_config)."""
    return [ln for ln in s.strip().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# _split_verb
# ---------------------------------------------------------------------------
check("split_verb set", sj._split_verb("set logical-systems X foo"),
      ("set", "logical-systems X foo"))
check("split_verb deactivate", sj._split_verb("deactivate logical-systems X"),
      ("deactivate", "logical-systems X"))
check("split_verb tanpa-verb", sj._split_verb("logical-systems X"),
      (None, "logical-systems X"))
check("split_verb kata-tunggal", sj._split_verb("set"),
      (None, "set"))   # tak ada path setelah verb → dianggap bukan verb

# ---------------------------------------------------------------------------
# _is_excluded — scope all vs state
# ---------------------------------------------------------------------------
pat_all = [("all", "logical-systems BGP-SCRIPT")]
pat_state = [("state", "logical-systems BGP-SCRIPT")]

# scope all → apa pun verb-nya, path di bawahnya dilindungi
check("excl all: set di bawah → dilindungi",
      sj._is_excluded("set", "logical-systems BGP-SCRIPT protocols bgp x", pat_all), True)
check("excl all: deactivate → dilindungi",
      sj._is_excluded("deactivate", "logical-systems BGP-SCRIPT", pat_all), True)
check("excl all: path lain → tidak",
      sj._is_excluded("set", "logical-systems BGP-OTHER x", pat_all), False)

# scope state → HANYA activate/deactivate yang dilindungi; set/delete lolos (ikut sync)
check("excl state: deactivate → dilindungi",
      sj._is_excluded("deactivate", "logical-systems BGP-SCRIPT", pat_state), True)
check("excl state: activate → dilindungi",
      sj._is_excluded("activate", "logical-systems BGP-SCRIPT", pat_state), True)
check("excl state: set isi → TIDAK dilindungi (ikut sync)",
      sj._is_excluded("set", "logical-systems BGP-SCRIPT protocols bgp x", pat_state), False)
check("excl state: prefix persis, set → tidak dilindungi",
      sj._is_excluded("set", "logical-systems BGP-SCRIPT", pat_state), False)

# ---------------------------------------------------------------------------
# _collapse_deletes — inti perbaikan
# ---------------------------------------------------------------------------
# Kasus BGP-CORE: 4 leaf di Backup, Master tak punya BGP-CORE tapi punya
# group lain di bawah 'protocols bgp' → runtuh HANYA sampai group BGP-CORE.
del_paths = [
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-CORE type external",
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-CORE family inet unicast",
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-CORE peer-as 6558",
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-CORE neighbor 10.10.100.10 export BGP-DOUBLE-IP",
]
master_paths = {
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-DOUBLE-IP type external",
    "logical-systems BGP-DOUBLE-IP protocols bgp group BGP-DOUBLE-IP peer-as 6555",
}
check("collapse: 4 leaf → 1 delete group",
      sj._collapse_deletes(del_paths, master_paths, []),
      ["delete logical-systems BGP-DOUBLE-IP protocols bgp group BGP-CORE"])

# Master benar-benar KOSONG untuk LS itu → runtuh sampai nama LS (bukan ke akar).
check("collapse: LS lenyap total → delete di level LS",
      sj._collapse_deletes(
          ["logical-systems GONE protocols bgp group G type external",
           "logical-systems GONE routing-options autonomous-system 1"],
          {"logical-systems KEEP x"}, []),
      ["delete logical-systems GONE"])

# Exclude scope-state melindungi STATUS tapi TIDAK menghalangi runtuh isi:
# subtree BGP-SCRIPT boleh diruntuhkan karena patternnya 'state' (bukan 'all').
# Namun _collapse_deletes memakai daftar (scope,prefix) apa adanya lewat exclude_under,
# yang mengecek prefix TANPA melihat scope → jadi ia menahan runtuh. Uji perilaku nyata:
check("collapse: exclude prefix menahan runtuh ke dalam subtree-nya",
      sj._collapse_deletes(
          ["logical-systems A protocols bgp group X foo",
           "logical-systems A protocols bgp group X bar"],
          set(),
          [("all", "logical-systems A protocols bgp group X bar")]),
      # 'bar' tertahan exclude → tetap leaf; 'foo' boleh runtuh, tapi induknya
      # 'group X' mengandung exclude → tertahan, naik selama tak menyentuh exclude.
      ["delete logical-systems A protocols bgp group X foo",
       "delete logical-systems A protocols bgp group X bar"])

# Dedup hierarkis: anak di bawah induk yang sudah dipilih dibuang.
check("collapse: dedup anak-di-bawah-induk",
      sj._collapse_deletes(
          ["logical-systems GONE a b c",
           "logical-systems GONE a b",
           "logical-systems GONE x"],
          {"logical-systems KEEP y"}, []),
      ["delete logical-systems GONE"])

# ---------------------------------------------------------------------------
# compute_diff — skenario end-to-end
# ---------------------------------------------------------------------------

# 1) Identik → tak ada perubahan
m = L("set logical-systems A protocols bgp group G peer-as 1")
b = L("set logical-systems A protocols bgp group G peer-as 1")
check("diff: identik → kosong", sj.compute_diff(m, b, []), ([], []))

# 2) Master punya lebih → ADD
m = L("""
set logical-systems A x
set logical-systems A y
""")
b = L("set logical-systems A x")
adds, rem = sj.compute_diff(m, b, [])
check("diff: tambah y", (adds, rem), (["set logical-systems A y"], []))

# 3) Backup punya lebih → DELETE runtuh ke container
m = L("set logical-systems A protocols bgp group KEEP peer-as 1")
b = L("""
set logical-systems A protocols bgp group KEEP peer-as 1
set logical-systems A protocols bgp group DROP type external
set logical-systems A protocols bgp group DROP peer-as 2
set logical-systems A protocols bgp group DROP neighbor 1.1.1.1 export P
""")
adds, rem = sj.compute_diff(m, b, [])
check("diff: hapus group DROP (runtuh)", (adds, rem),
      ([], ["delete logical-systems A protocols bgp group DROP"]))

# 4) Ganti nilai leaf: hapus lama + set baru (delete duluan)
m = L("set logical-systems A protocols bgp group G peer-as 100")
b = L("set logical-systems A protocols bgp group G peer-as 200")
adds, rem = sj.compute_diff(m, b, [])
check("diff: ganti peer-as → add baru",
      adds, ["set logical-systems A protocols bgp group G peer-as 100"])
check("diff: ganti peer-as → delete lama",
      rem, ["delete logical-systems A protocols bgp group G peer-as 200"])

# 5) Status: Backup deactivate, Master aktif → activate
m = L("set logical-systems A x")
b = L("""
set logical-systems A x
deactivate logical-systems A
""")
adds, rem = sj.compute_diff(m, b, [])
check("diff: aktifkan A (master aktif)", (adds, rem),
      ([], ["activate logical-systems A"]))

# 6) Status: Master deactivate, Backup aktif → deactivate (add apa adanya)
m = L("""
set logical-systems A x
deactivate logical-systems A
""")
b = L("set logical-systems A x")
adds, rem = sj.compute_diff(m, b, [])
check("diff: nonaktifkan A (master nonaktif)", (adds, rem),
      (["deactivate logical-systems A"], []))

# 7) Exclude scope=state: status BGP-SCRIPT dijaga, isinya tetap disync
m = L("set logical-systems BGP-SCRIPT protocols bgp group G peer-as 1")
b = L("""
set logical-systems BGP-SCRIPT protocols bgp group G peer-as 1
set logical-systems BGP-SCRIPT protocols bgp group OLD type external
deactivate logical-systems BGP-SCRIPT
""")
adds, rem = sj.compute_diff(m, b, pat_state)
check("diff+excl-state: status dijaga (tak ada activate), isi tetap dihapus",
      (adds, rem),
      ([], ["delete logical-systems BGP-SCRIPT protocols bgp group OLD"]))

# 8) Exclude scope=all: seluruh BGP-SCRIPT tak tersentuh
m = L("")  # master tak punya BGP-SCRIPT sama sekali
b = L("""
set logical-systems BGP-SCRIPT protocols bgp group OLD type external
deactivate logical-systems BGP-SCRIPT
""")
# tapi master kosong total → guard 'master tak punya LS' di sync_config_diff yang
# menangani; di level compute_diff, exclude-all harus menahan semua.
adds, rem = sj.compute_diff(m, b, pat_all)
check("diff+excl-all: BGP-SCRIPT tak tersentuh", (adds, rem), ([], []))

# 9) Delete container TIDAK menelan status yang sudah tercakup:
#    kalau group dihapus, activate/deactivate di bawahnya harus ikut lenyap (bukan error).
#    Master punya group KEEP di bawah 'protocols bgp' → collapse berhenti di group DROP
#    (persis skenario BGP-CORE), bukan naik ke 'protocols'.
m = L("set logical-systems A protocols bgp group KEEP peer-as 9")
b = L("""
set logical-systems A protocols bgp group KEEP peer-as 9
set logical-systems A protocols bgp group DROP peer-as 2
deactivate logical-systems A protocols bgp group DROP
""")
adds, rem = sj.compute_diff(m, b, [])
check("diff: status di bawah container-terhapus dibuang",
      (adds, rem),
      ([], ["delete logical-systems A protocols bgp group DROP"]))

# 9b) Master KOSONG di bawah suatu subtree → collapse boleh naik lebih tinggi (valid).
m = L("set logical-systems A other z")
b = L("""
set logical-systems A other z
set logical-systems A protocols bgp group DROP peer-as 2
""")
adds, rem = sj.compute_diff(m, b, [])
check("diff: master kosong di 'protocols' → runtuh ke 'protocols' (atomik, valid)",
      (adds, rem),
      ([], ["delete logical-systems A protocols"]))

# ---------------------------------------------------------------------------
# load_exclude_patterns — parsing verb→scope
# ---------------------------------------------------------------------------
def write_tmp(text):
    fd, path = tempfile.mkstemp(suffix=".conf")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path

p = write_tmp("""
# komentar
deactivate logical-systems BGP-SCRIPT

logical-systems BGP-FULL
set logical-systems BGP-SET foo
""")
try:
    got = sj.load_exclude_patterns(p)
finally:
    os.remove(p)
check("load_exclude: verb→scope",
      got,
      [("state", "logical-systems BGP-SCRIPT"),
       ("all", "logical-systems BGP-FULL"),
       ("all", "logical-systems BGP-SET foo")])

check("load_exclude: file tak ada → kosong",
      sj.load_exclude_patterns("/tidak/ada/file.conf"), [])

# ---------------------------------------------------------------------------
print("\n" + ("❌ ADA GAGAL: " + ", ".join(_fails) if _fails else "✅ SEMUA LOLOS"))
sys.exit(1 if _fails else 0)

#!/usr/bin/env python3
"""prepare_hud.py — convert HUD xlsx downloads into the slim committed CSVs.

huduser.gov blocks non-browser fetchers (HTTP 202 empty body) and CI runs on
datacenter IPs, so the pipeline NEVER fetches HUD directly. Instead, run this
once a year on a normal machine and commit the outputs.

Annual refresh (browser UA required):
  curl -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 \
(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36" \
    -o safmr.xlsx https://www.huduser.gov/portal/datasets/fmr/fmr2026/fy2026_safmrs_revised.xlsx
  curl -A "<same UA>" \
    -o psh.xlsx "https://www.huduser.gov/portal/datasets/pictures/files/COUNTY_2025_2020census.xlsx"
  python scripts/prepare_hud.py safmr.xlsx psh.xlsx

Outputs (committed):
  src/remon/data/hud_safmr_<FY>.csv       zip,safmr_2br,safmr_3br
  src/remon/data/hud_psh_county_<YR>.csv  fips,units_all,units_hcv
"""
import re
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "src" / "remon" / "data"

# HUD sentinel values in PSH: -1 missing, -4 suppressed (<11 families), -5 non-reporting.
PSH_SENTINELS = {-1, -4, -5}


def prepare_safmr(path: Path) -> Path:
    df = pd.read_excel(path, sheet_name="SAFMRs")
    # The ZIP column header literally contains a newline ("ZIP\nCode").
    cols = {re.sub(r"\s+", " ", str(c)).strip(): c for c in df.columns}
    zip_col, br2, br3 = cols["ZIP Code"], cols["SAFMR 2BR"], cols["SAFMR 3BR"]
    out = pd.DataFrame({
        "zip": df[zip_col].astype(str).str.strip().str.zfill(5),
        "safmr_2br": pd.to_numeric(df[br2], errors="coerce"),
        "safmr_3br": pd.to_numeric(df[br3], errors="coerce"),
    }).dropna(subset=["safmr_2br"])
    # ZIPs repeat across FMR areas; 2BR/3BR values are identical across repeats.
    dup_check = out.groupby("zip")[["safmr_2br", "safmr_3br"]].nunique()
    conflicts = int((dup_check > 1).any(axis=1).sum())
    if conflicts:
        print(f"WARNING: {conflicts} ZIPs have conflicting SAFMRs — keeping first")
    out = out.drop_duplicates("zip")
    year = re.search(r"(20\d{2})", path.name)
    fy = year.group(1) if year else "latest"
    dest = DATA / f"hud_safmr_{fy}.csv"
    out.to_csv(dest, index=False)
    print(f"wrote {dest} ({len(out)} ZIPs)")
    return dest


def prepare_psh(path: Path) -> Path:
    df = pd.read_excel(path, sheet_name="COUNTY_EXTRACT")
    df["code"] = df["code"].astype(str).str.strip().str.zfill(5)
    # Drop HUD's per-state suppressed-remainder pseudo-counties ("09XXX" etc.) —
    # they never match a real FIPS and would double-count in any aggregation.
    df = df[df["code"].str.fullmatch(r"\d{5}")]
    units = pd.to_numeric(df["total_units"], errors="coerce")
    units = units.where(~units.isin(PSH_SENTINELS))
    df["units_clean"] = units

    is_all = df["program_label"] == "Summary of All HUD Programs"
    # HCV summary row only — TBV/PBV sub-program rows are subsets (double-count).
    is_hcv = (df["program_label"] == "Housing Choice Vouchers") & (
        df["sub_program"].astype(str).isin(("N/A", "nan", ""))
    )
    all_u = df[is_all].drop_duplicates("code").set_index("code")["units_clean"]
    hcv_u = df[is_hcv].drop_duplicates("code").set_index("code")["units_clean"]
    out = pd.DataFrame({"units_all": all_u, "units_hcv": hcv_u})
    out.index.name = "fips"
    year = re.search(r"(20\d{2})", path.name)
    yr = year.group(1) if year else "latest"
    dest = DATA / f"hud_psh_county_{yr}.csv"
    out.to_csv(dest)
    print(f"wrote {dest} ({len(out)} counties)")
    return dest


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: prepare_hud.py <safmr.xlsx> <psh_county.xlsx>")
    prepare_safmr(Path(sys.argv[1]))
    prepare_psh(Path(sys.argv[2]))

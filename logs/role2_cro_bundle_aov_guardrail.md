# Role 2 (CRO & Lifecycle) <-> Role 3 (Shopify Ops) Bundle & AOV Guardrail Audit
- **Generated At**: `2026-11-02 08:46:55 UTC`
- **Objective**: Verify that *The Complete Transformer Dining Bundle (Table 4.0 + Bench 4.0 + Coffee Table + 2x Chair Sets)* maintains an exact **18%–22% Bundle Savings** across all 5 regional currencies and never violates `Price < Compare-At`.

| Finish Tier | Standalone Sum (Table+Bench+Coffee+4 Chairs) | Bundle Price | Customer Savings (%) | Compare-At Integrity | CRO Test Gate |
|---|---|---|---|---|---|
| **Scandinavian Oak Suite** (`TT-BND40-OAK-INT`) | 113,500,000 ₫ (`$4,545`) | **89,900,000 ₫** (`$3,599`) | **Save 20.8%** | `89.9M < 114.5M` (PASS) | **SAFE_FOR_AB_TEST** |
| **Smoked Hickory Suite** (`TT-BND40-HCK-INT`) | 113,500,000 ₫ (`$4,545`) | **89,900,000 ₫** (`$3,599`) | **Save 20.8%** | `89.9M < 114.5M` (PASS) | **SAFE_FOR_AB_TEST** |
| **American Walnut Suite** (`TT-BND40-WAL-INT`) | 120,700,000 ₫ (`$4,825`) | **94,900,000 ₫** (`$3,799`) | **Save 21.4%** | `94.9M < 119.5M` (PASS) | **SAFE_FOR_AB_TEST** |
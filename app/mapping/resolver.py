"""Identity and unit resolution.

This module turns *source-specific* identifiers into *canonical* identity, and
source units into a declared base unit. It is deliberately the only place that
knows about alias crosswalks, so the detection rule never joins on raw IDs or
display names.

Authoritative-source policy (Task A.6):
  * Product canonical identity + UOM factor : product_aliases.csv (reference data)
  * Location canonical identity              : location_aliases.csv
  * Employee canonical location              : HRMS code -> location_aliases.csv
  * PO ordered quantity / status / deadline  : ERP (purchase_order_lines.csv)
  * Receipt accepted quantity / posting      : WMS (goods_receipt_lines.csv)

A missing or ambiguous mapping is never guessed. The resolver returns a typed
*failure* that the caller records as a data-quality issue, so the row is visible
rather than silently dropped or mis-joined.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import LocationAlias, ProductAlias


class MappingError(Exception):
    """Raised when identity/UOM cannot be resolved. Carries a stable reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class ProductResolution:
    canonical_product_id: str
    base_uom: str
    to_base_factor: float


@dataclass(frozen=True)
class LocationResolution:
    canonical_location_id: str
    region_id: Optional[str]


class Resolver:
    """Loads the crosswalks once per unit of work and resolves against them.

    Ambiguity rule: if a single (source_system, external_id) maps to more than one
    canonical id, that is a reference-data conflict, not something to pick from.
    """

    def __init__(self, session: Session, tenant_id: str):
        self._session = session
        self._tenant_id = tenant_id
        self._product_cache: dict[tuple[str, str], list[ProductAlias]] = {}
        self._location_cache: dict[tuple[str, str], list[LocationAlias]] = {}

    # -- products -----------------------------------------------------------
    def resolve_product(self, source_system: str, external_item_id: str) -> ProductResolution:
        key = (source_system.upper(), external_item_id)
        rows = self._product_cache.get(key)
        if rows is None:
            rows = list(
                self._session.scalars(
                    select(ProductAlias).where(
                        ProductAlias.tenant_id == self._tenant_id,
                        ProductAlias.source_system == source_system.upper(),
                        ProductAlias.external_item_id == external_item_id,
                    )
                )
            )
            self._product_cache[key] = rows

        if not rows:
            raise MappingError(
                "unmapped_product",
                f"no product alias for {source_system}:{external_item_id}",
            )
        canon = {(r.canonical_product_id, r.base_uom, r.to_base_factor) for r in rows}
        if len(canon) > 1:
            raise MappingError(
                "ambiguous_product",
                f"product alias for {source_system}:{external_item_id} is ambiguous: {canon}",
            )
        r = rows[0]
        if r.to_base_factor <= 0:
            raise MappingError(
                "invalid_uom_factor",
                f"non-positive to_base_factor for {source_system}:{external_item_id}",
            )
        return ProductResolution(r.canonical_product_id, r.base_uom, r.to_base_factor)

    def convert_to_base(self, resolution: ProductResolution, qty: float) -> float:
        """Convert a source quantity into the product's base unit.

        We never compare raw quantities across units (Task A.3). 10 BOX with a
        factor of 12 becomes 120 EA before any comparison happens.
        """
        return qty * resolution.to_base_factor

    # -- locations ----------------------------------------------------------
    def resolve_location(self, source_system: str, external_location_id: str) -> LocationResolution:
        key = (source_system.upper(), external_location_id)
        rows = self._location_cache.get(key)
        if rows is None:
            rows = list(
                self._session.scalars(
                    select(LocationAlias).where(
                        LocationAlias.tenant_id == self._tenant_id,
                        LocationAlias.source_system == source_system.upper(),
                        LocationAlias.external_location_id == external_location_id,
                    )
                )
            )
            self._location_cache[key] = rows

        if not rows:
            raise MappingError(
                "unmapped_location",
                f"no location alias for {source_system}:{external_location_id}",
            )
        canon = {r.canonical_location_id for r in rows}
        if len(canon) > 1:
            raise MappingError(
                "ambiguous_location",
                f"location alias for {source_system}:{external_location_id} is ambiguous: {canon}",
            )
        r = rows[0]
        return LocationResolution(r.canonical_location_id, r.region_id)

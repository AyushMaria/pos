"""Catalogue lookup and search — architecture §4, §10.1.

Two ways in, and both matter. A barcode is faster when there is one; search is
the *only* route for the 38.6% of the pilot catalogue that has no barcode at
all, which makes it a primary path rather than a fallback.

Lookup never raises for a bad code. A cashier holding an unreadable label
needs to be told which problem they have — mistyped digits, or a product this
shop does not stock — because the next action is different.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentSession, get_catalog_repository, require
from app.api.schemas import LookupResponse, MoneyOut, ProductOut, SearchResponse
from app.data.repositories.catalog import CatalogProduct, CatalogRepository
from app.domain import permissions
from app.domain.barcode import parse
from app.domain.identity import Session

router = APIRouter(prefix="/catalog", tags=["catalog"])

CatalogRepo = Annotated[CatalogRepository, Depends(get_catalog_repository)]


def _to_product_out(product: CatalogProduct) -> ProductOut:
    return ProductOut(
        product_id=product.product_id,
        sku=product.sku,
        name=product.name,
        short_name=product.display_name,
        uom=product.uom,
        is_weighed=product.is_weighed,
        tax_code=product.tax_code.code,
        rate_bp=product.tax_code.rate_bp,
        unit_price=MoneyOut.of(product.unit_price),
        pack_size=product.pack_size,
        barcode=product.barcode,
    )


@router.get("/lookup", response_model=LookupResponse)
def lookup(
    barcode: str,
    catalog: CatalogRepo,
    session: Annotated[Session, Depends(require(permissions.PRODUCT_READ))],
) -> LookupResponse:
    """Parse a code and resolve it, in one round trip.

    Parsing is pure and shared with receiving (architecture §10.1); resolution
    needs the catalogue. Both happen here so the register makes one call per
    scan and stays inside the 150 ms budget.
    """
    scan = parse(barcode)

    if not scan.is_usable or scan.lookup_key is None:
        return LookupResponse(
            raw=barcode,
            symbology=scan.symbology,
            resolved=False,
            error=scan.error,
        )

    product = catalog.by_lookup_key(scan.lookup_key)
    return LookupResponse(
        raw=barcode,
        symbology=scan.symbology,
        resolved=product is not None,
        product=None if product is None else _to_product_out(product),
        qty_milli=scan.qty_milli,
        is_case=scan.is_case,
        error=None if product is not None else "no product with that code",
    )


@router.get("/search", response_model=SearchResponse)
def search(
    catalog: CatalogRepo,
    session: Annotated[Session, Depends(require(permissions.PRODUCT_READ))],
    q: str = Query(min_length=1, description="Name or SKU fragment"),
    limit: int = Query(default=25, ge=1, le=100),
) -> SearchResponse:
    return SearchResponse(
        query=q,
        results=[_to_product_out(product) for product in catalog.search(q, limit)],
    )


@router.get("/size")
def catalog_size(catalog: CatalogRepo, session: CurrentSession) -> dict[str, int]:
    """How many products this terminal can sell. Used by the diagnostics view."""
    return {"products": catalog.count()}

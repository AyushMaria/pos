"""Selling something the till has never seen - architecture 10.2, 9.1.

A customer is at the counter with an item nothing recognises. The basket is
open and there is a queue. The cashier types what it is and what it costs, the
sale completes, and somebody sorts the catalogue out later.

**The line points at a placeholder, not at a new product.** `sale_lines.
product_id` is `not null references public.products(id)`, so a line naming a
product the cloud has never heard of fails that foreign key and quarantines
the entire sale - taken, printed and paid for, then silently refused. The
item's real identity travels on the line instead, in `description` and
`barcode_scanned`, which exist for exactly this.

This module holds the one thing both sides must agree on: which product row
that is. `supabase/migrations/0014` inserts it with this id, and a test fails
if the two ever drift. A terminal guessing a different id would write lines
that fail the very foreign key the design exists to avoid.
"""

from __future__ import annotations

#: The catalogue row every unlisted line references. Seeded by 0014, pulled
#: down like any other product, and never sold *as itself* - it is a stand-in.
UNLISTED_PRODUCT_ID = "018f0000-0000-7000-8000-0000000000ff"

#: Not tracked in stock, and cannot be: one row standing in for many unrelated
#: products would accumulate a level that means nothing. When the real product
#: is created it needs an opening count, which is a stock-take either way.
UNLISTED_SKU = "SKU-UNLISTED"

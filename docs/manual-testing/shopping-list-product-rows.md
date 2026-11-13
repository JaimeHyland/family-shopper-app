# One shopping-list row per product

## Preconditions

- Use a development family with two members, an open shopping list and
  three units of Chocolate bar.
- Open Shopping list as each member in separate browsers.

## Checks

1. Chocolate bar appears once, with total 3, On the list 3, In a basket 0,
   Bought 0, using the same readable light badge as Products.
2. Put one unit in each member's basket. The product still appears once
   on both browsers without reloading. Counts are 3 / 1 / 2 / 0, with
   each member named alongside their basket quantity.
3. Put the last unassigned unit in a basket. Only now is Put in basket
   disabled. Return one unit: it becomes enabled again.
4. Return removes only a unit in the signed-in member's basket; it is
   disabled for a member with no unit of this product in their basket.
5. Buy one member's basket. The same row shows bought counts attributed
   to that member and preserves the other member's basket ownership.
   Buy all remaining units: the product is hidden until Show bought items
   is selected, then appears once in the main list with no purchase controls.
6. Check all grouping modes, live updates with the filter modal open,
   keyboard focus after an action, and a narrow/mobile window.
7. Change preferred shops on individual units. Shop filters continue to
   count matching units only. A visible basket-only subset can still use
   Put in basket if an eligible unit exists outside that filter.
8. Add or remove an ordinary unit and confirm/cancel the final-unit dialog.
   Other members' basket and bought units must remain unchanged.
9. A cancelled unit appears in the breakdown without hiding other active
   units. Fully cancelled products use the existing Show cancelled items
   toggle. Restoring a unit updates the row without duplicating it.
10. Check English, German and Spanish labels, long member names, distinct
    variants, another family's list and closing/carrying forward the list.

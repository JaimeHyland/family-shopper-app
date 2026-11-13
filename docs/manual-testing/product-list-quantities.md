# Products-page shopping-list quantities

## Preconditions

- Use a development family with two members and an open shopping list.
- Have one product absent from the list and another (e.g. Chocolate bar)
  requested three times using the existing add/increase-quantity flow.
- Open Products in one browser and Shopping list in another.

## Checks

1. The absent product retains its existing row appearance, add action and
   separate details link, without a visible quantity badge.
2. Chocolate bar shows total 3, On the list 3, In a basket 0, Bought 0.
3. Put two units in baskets, including one in the other member's basket.
   Products updates without reloading: total 3, On the list 1,
   In a basket 2, Bought 0.
4. Remove one unit from a basket. Counts become 3 / 2 / 1 / 0.
5. Buy the remaining basket. Counts become 3 / 2 / 0 / 1.
   Buy the remaining units: total 3 and Bought 3 remain visible.
6. Check all grouping modes and a narrow/mobile window: the compact
   breakdown wraps, remains readable and adds no new controls.
7. Increase a quantity or remove an eligible unit: the counts update live.
   Cancelled units still count as On the list, preserving existing behaviour.
8. Close the list using the existing workflow. Only units carried to the
   new list count; bought units from the closed list no longer appear.
9. Check another family and a distinct variant product: counts stay separate.

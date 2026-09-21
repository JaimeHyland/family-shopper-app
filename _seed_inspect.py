from shopping_list.models import Family, Category, Product, FamilyMembership

print("=== FAMILIES MATCHING 'cobh' OR 'hyland' (case-insensitive) ===")
qs = Family.objects.filter(name__icontains="cobh") | Family.objects.filter(name__icontains="hyland")
for f in qs.distinct().order_by("id"):
    print(f"id={f.id} name={f.name!r} public_id={f.public_id}")

print()
print("=== ALL FAMILIES (id, name) ===")
for f in Family.objects.all().order_by("id"):
    print(f"id={f.id} name={f.name!r}")

print()
print("=== DONE ===")

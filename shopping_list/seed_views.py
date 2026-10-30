from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View

from utilities.utils import has_family_permission
from .seed_lists import import_seed_products, load_seed_lists
from .views import FamilyMembershipRequiredMixin


class SeedSelectionForm(forms.Form):
    products = forms.MultipleChoiceField(required=False)

    def __init__(self, seed, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["products"].choices = [
            (name, name) for category in seed["categories"]
            for name in category["products"]
        ]


@method_decorator(login_required, name="dispatch")
class SeedListView(FamilyMembershipRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        seeds = load_seed_lists()
        seed_id = request.GET.get("seed", next(iter(seeds)))
        seed = self.get_seed(seeds, seed_id)
        selected = {
            name for category in seed["categories"]
            for name in category["products"]
        }
        return self.show(
            request, seeds, seed_id, SeedSelectionForm(seed), selected,
        )

    def post(self, request, *args, **kwargs):
        seeds = load_seed_lists()
        seed_id = request.POST.get("seed", "")
        seed = self.get_seed(seeds, seed_id)
        form = SeedSelectionForm(seed, request.POST)
        if not form.is_valid():
            allowed = {name for name, _ in form.fields["products"].choices}
            selected = set(request.POST.getlist("products")) & allowed
            return self.show(
                request, seeds, seed_id, form, selected, status=400,
            )
        selected = set(form.cleaned_data["products"])
        action = request.POST.get("action", "review")
        if action in ("review", "edit"):
            return self.show(
                request, seeds, seed_id, form, selected,
                review=action == "review",
            )
        if action != "import":
            return HttpResponseBadRequest("Unknown seed-list action.")
        if not has_family_permission(request, "can_add_catalogue"):
            raise PermissionDenied
        try:
            result = import_seed_products(request, seed_id, selected)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.show(
                request, seeds, seed_id, form, selected, status=400,
            )
        if selected:
            messages.success(
                request,
                f"Added to your product catalogue: {len(result.created)}. "
                f"Already in your catalogue: {len(result.reused)}. "
                f"Nothing was added to your shopping list.",
            )
        else:
            messages.info(request, "No products selected; nothing was added.")
        return redirect("product_list", family_slug=request.family.slug)

    @staticmethod
    def get_seed(seeds, seed_id):
        if seed_id not in seeds:
            raise Http404("No such seed list.")
        return seeds[seed_id]

    @staticmethod
    def show(request, seeds, seed_id, form, selected, review=False, status=200):
        seed = seeds[seed_id]
        categories = seed["categories"]
        if review:
            categories = [
                {"name": category["name"], "products": [
                    name for name in category["products"] if name in selected
                ]}
                for category in categories
                if any(name in selected for name in category["products"])
            ]
        return render(request, "shopping_list/seed_lists.html", {
            "seeds": seeds, "seed_id": seed_id, "seed": seed,
            "categories": categories, "form": form,
            "selected_names": selected, "review": review,
        }, status=status)

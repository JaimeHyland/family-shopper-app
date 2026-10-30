document.addEventListener('DOMContentLoaded', function () {
    const form = document.getElementById('seed-selection-form');
    if (!form) {
        return;
    }
    const all = form.querySelector('[data-seed-all]');
    const groups = Array.from(
        form.querySelectorAll('[data-seed-category-group]')
    );
    const products = Array.from(form.querySelectorAll('[data-seed-product]'));
    const count = document.getElementById('seed-selection-count');

    function updateCheckbox(checkbox, children) {
        const selected = children.filter(child => child.checked).length;
        checkbox.checked = selected === children.length && selected > 0;
        checkbox.indeterminate = selected > 0 && selected < children.length;
    }

    function syncSelection() {
        for (const group of groups) {
            updateCheckbox(
                group.querySelector('[data-seed-category]'),
                Array.from(group.querySelectorAll('[data-seed-product]'))
            );
        }
        updateCheckbox(all, products);
        count.textContent = 'Selected products: ' +
            products.filter(product => product.checked).length;
    }

    form.addEventListener('change', function (event) {
        if (event.target === all) {
            products.forEach(product => { product.checked = all.checked; });
        } else if (event.target.matches('[data-seed-category]')) {
            const group = event.target.closest('[data-seed-category-group]');
            group.querySelectorAll('[data-seed-product]').forEach(product => {
                product.checked = event.target.checked;
            });
        }
        syncSelection();
    });
    syncSelection();
});

// Run with: node --test shopping_list/tests_unit_controls.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

function compatibilitySource() {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    return template.slice(template.indexOf('function getBootstrapInstance('),
        template.indexOf('function submitForm()'));
}

test('product quantity breakdown updates live and hides only when total is zero', () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(
        template.indexOf('function updateProductListStatus('),
        template.indexOf('// Tracks whether the "list complete"'),
    );
    const hidden = new Set(['d-none']);
    const elements = Object.fromEntries([
        '.on-list-quantity', '.on-list-unbasketed-quantity',
        '.on-list-basket-quantity', '.on-list-bought-quantity',
    ].map(selector => [selector, {textContent: ''}]));
    elements['.on-list-badge'] = {
        classList: {
            add: name => hidden.add(name),
            remove: name => hidden.delete(name),
        },
    };
    let rendered = true;
    const context = vm.createContext({
        document: {
            querySelector: selector => {
                assert.equal(selector, ".product-row[data-product-id='7']");
                return rendered ? {
                    querySelector: name => elements[name],
                } : null;
            },
        },
    });
    vm.runInContext(source, context);
    for (const [total, unbasketed, basket, bought] of [
        [3, 1, 2, 0], [3, 1, 1, 1], [3, 0, 0, 3], [0, 0, 0, 0],
    ]) {
        context.updateProductListStatus({
            product_id: 7, list_total_quantity: total,
            list_unbasketed_quantity: unbasketed,
            list_basket_quantity: basket, list_bought_quantity: bought,
        });
        assert.equal(elements['.on-list-quantity'].textContent, total);
        assert.equal(elements['.on-list-unbasketed-quantity'].textContent, unbasketed);
        assert.equal(elements['.on-list-basket-quantity'].textContent, basket);
        assert.equal(elements['.on-list-bought-quantity'].textContent, bought);
        assert.equal(hidden.has('d-none'), total === 0);
    }
    rendered = false;
    assert.doesNotThrow(() => context.updateProductListStatus({product_id: 7}));
});

test('empty category/shop headings are hidden once every row beneath them is hidden', () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(
        template.indexOf('function toggleCancelledVisibility'),
        template.indexOf('// Both visibility filters'),
    );

    function makeClassList(initial = []) {
        const set = new Set(initial);
        return {
            contains: name => set.has(name),
            toggle: (name, force) => {
                if (force) { set.add(name); } else { set.delete(name); }
            },
        };
    }

    function makeList(rows) {
        return {
            classList: makeClassList(['hide-cancelled', 'hide-bought']),
            children: rows.map(row => ({
                dataset: row.heading ? {groupHeading: row.heading} : {},
                classList: makeClassList(row.classes || []),
            })),
        };
    }

    let list, toggle, boughtToggle;
    const context = vm.createContext({
        document: {
            getElementById: id => (
                id === 'shopping_list' ? list
                    : id === 'cancelled-toggle' ? toggle
                        : id === 'bought-toggle' ? boughtToggle : null
            ),
        },
    });
    vm.runInContext(source, context);

    // Category A keeps one visible item; Category B's only item is
    // cancelled and hidden by default -> its heading must hide too.
    list = makeList([
        {heading: 'primary'}, {classes: []},
        {heading: 'primary'}, {classes: ['cancelled-list-item']},
    ]);
    context.syncGroupHeadingVisibility();
    assert.equal(list.children[0].classList.contains('d-none'), false);
    assert.equal(list.children[2].classList.contains('d-none'), true);

    // Toggling "Show cancelled items" on must reveal that heading again.
    toggle = {checked: true};
    context.toggleCancelledVisibility();
    assert.equal(list.classList.contains('hide-cancelled'), false);
    assert.equal(list.children[2].classList.contains('d-none'), false);

    // Two-level grouping: an empty sub-heading hides even while its
    // parent heading stays visible because of its other sub-group.
    list = makeList([
        {heading: 'primary'},
        {heading: 'secondary'}, {classes: ['cancelled-list-item']},
        {heading: 'secondary'}, {classes: []},
    ]);
    toggle = {checked: false};
    context.toggleCancelledVisibility();
    assert.equal(list.children[0].classList.contains('d-none'), false);
    assert.equal(list.children[1].classList.contains('d-none'), true);
    assert.equal(list.children[3].classList.contains('d-none'), false);

    list = makeList([
        {heading: 'primary'}, {classes: ['bought-only-list-item']},
        {heading: 'primary'}, {classes: []},
    ]);
    boughtToggle = {checked: false};
    context.toggleBoughtVisibility();
    assert.equal(list.children[0].classList.contains('d-none'), true);
    assert.equal(list.children[2].classList.contains('d-none'), false);
    boughtToggle.checked = true;
    context.toggleBoughtVisibility();
    assert.equal(list.children[0].classList.contains('d-none'), false);
});

test('Bootstrap 5.0.1 components reuse instances and preserve options', () => {
    const context = vm.createContext({});
    vm.runInContext(compatibilitySource(), context);
    for (const name of ['Collapse', 'Modal', 'Toast']) {
        const instances = new Map();
        let constructions = 0;
        class Component {
            static getInstance(element) { return instances.get(element); }
            constructor(element, options) {
                constructions++;
                this.options = options;
                instances.set(element, this);
            }
        }
        const element = {}, options = {toggle: false};
        const first = context.getBootstrapInstance(Component, element, options);
        assert.equal(first.options, options, name);
        assert.equal(context.getBootstrapInstance(Component, element), first, name);
        assert.equal(constructions, 1, name);
    }
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    assert.doesNotMatch(template, /\.getOrCreateInstance\(/);
});

test('bought filter toggles fully bought rows without a separate collapse', () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(template.indexOf('// Both visibility filters'),
        template.indexOf('// All quantities, attribution'));
    const events = {};
    const toggle = {checked: false, addEventListener: (name, fn) => { events[name] = fn; }};
    let boughtSyncs = 0, cancelledSyncs = 0;
    let initialize;
    const context = vm.createContext({
        document: {
            getElementById: id => id === 'bought-toggle' ? toggle : null,
            addEventListener: (name, fn) => { initialize = fn; },
        },
        toggleBoughtVisibility: () => { boughtSyncs++; },
        toggleCancelledVisibility: () => { cancelledSyncs++; },
    });
    vm.runInContext(source, context);
    initialize();
    assert.equal(boughtSyncs, 1);
    assert.equal(cancelledSyncs, 1);
    toggle.checked = true;
    events.change();
    assert.equal(boughtSyncs, 2);
    toggle.checked = false;
    events.change();
    assert.equal(boughtSyncs, 3);
    context.document.getElementById = () => null;
    assert.doesNotThrow(initialize);
    assert.equal(template.includes('bought-items-list'), false);
});

test('live product row refresh serializes updates and restores focus by product', async () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(
        template.indexOf('let rowRefreshRunning = false;'),
        template.indexOf('let finalUnitId = null;'),
    );
    let fetches = 0, resolveFirst, focuses = 0, headingSyncs = 0;
    const button = {
        dataset: {itemId: '17', basketAction: 'add_to_basket'},
        closest: () => ({dataset: {productId: '9', state: 'ordinary'}}),
    };
    const control = {disabled: false, focus: () => { focuses++; }};
    const selectors = [];
    const list = {
        innerHTML: '',
        contains: element => element === button,
        querySelector: selector => {
            selectors.push(selector);
            return selector === '[data-product-id="9"]' ? {
                querySelector: name => name === '[data-basket-action="add_to_basket"]:not(:disabled)'
                    ? control : null,
            } : null;
        },
    };
    const fields = {
        shopping_list: list, 'bought-count': {}, 'buy-my-basket-btn': {},
    };
    const response = rows => ({
        ok: true, json: async () => ({
            shopping_list_id: 7, rows_html: rows, bought_count: 1,
            has_basket_items: true, show_completion_suggestion: false,
        }),
    });
    const context = vm.createContext({
        document: {
            activeElement: button, body: {},
            getElementById: id => {
                assert.ok(Object.hasOwn(fields, id), `Unexpected element ${id}`);
                return fields[id];
            },
        },
        currentOpenListId: 7,
        window: {location: {href: 'http://localhost/list/', reload: () => assert.fail('Unexpected reload')}},
        URL, console,
        syncGroupHeadingVisibility: () => { headingSyncs++; },
        fetch: () => {
            fetches++;
            return fetches === 1 ? new Promise(resolve => { resolveFirst = resolve; })
                : Promise.resolve(response('latest'));
        },
    });
    vm.runInContext(source, context);
    const pending = context.refreshShoppingListRows();
    await context.refreshShoppingListRows();
    assert.equal(fetches, 1);
    resolveFirst(response('first'));
    await pending;
    assert.equal(fetches, 2);
    assert.equal(list.innerHTML, 'latest');
    assert.equal(fields['bought-count'].textContent, 1);
    assert.equal(fields['buy-my-basket-btn'].disabled, false);
    assert.equal(focuses, 2);
    assert.equal(headingSyncs, 2);
    assert.ok(selectors.includes('[data-product-id="9"]'));
    assert.ok(selectors.every(selector => !selector.includes('data-state')));
});

test('disabled controls, final-unit confirmation and dismissal', async () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(template.indexOf('function getBootstrapInstance('),
        template.indexOf('function buyMyBasket()')) + '\n' +
        template.slice(template.indexOf('let finalUnitId = null;'),
            template.indexOf('function showBasketBoughtModal'));
    const handlers = {}, events = {};
    const fields = {'item-id': {value: ''}, 'action-type': {value: ''}};
    const modal = {addEventListener: (name, fn) => { handlers[name] = fn; }};
    const form = {getAttribute: () => '/shopping-list/'};
    const calls = [];
    let shows = 0, refreshes = 0;
    let response = {ok: false, status: 409,
        json: async () => ({confirmation_required: true, item_id: 42})};
    const context = vm.createContext({
        document: {
            getElementById: id => id === 'finalUnitModal' ? modal :
                id === 'confirm-final-unit' ? {
                    addEventListener: (name, fn) => { handlers.confirm = fn; },
                } : id === 'shopping-list-form' ? form : fields[id],
            addEventListener: (name, fn) => { events[name] = fn; },
        },
        bootstrap: {Modal: class {
            static getInstance() { return null; }
            show() { shows++; }
            hide() { handlers['hidden.bs.modal'](); }
        }},
        FormData: class {},
        fetch: () => {
            calls.push([fields['item-id'].value, fields['action-type'].value]);
            return Promise.resolve(response);
        },
        getCSRFToken: () => '',
        refreshShoppingListRows: () => { refreshes++; },
        pendingRowFocus: null,
        console,
        alert: () => assert.fail('Unexpected alert'),
        window: {location: {reload: () => assert.fail('Unexpected reload')}},
    });
    vm.runInContext(source, context);
    const button = {disabled: false, dataset: {itemId: '42', basketAction: 'remove_unit'}};
    const click = target => events.click({target: {closest: () => target}});
    const settle = () => new Promise(resolve => setImmediate(resolve));

    click({disabled: true}); // Native disabled button, even if invoked synthetically.
    click(null); // Hover/focus wrapper has no action and cannot submit.
    await settle();
    assert.equal(calls.length, 0);
    click(button);
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(shows, 1);
    handlers['hidden.bs.modal'](); // Close, Escape, backdrop or "Keep item".
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(refreshes, 1);
    handlers.confirm();
    assert.equal(calls.length, 1); // Dismissal clears the pending action.

    button.disabled = false;
    click(button);
    await settle();
    assert.equal(shows, 2);
    response = {ok: true, status: 200};
    handlers.confirm();
    await settle();
    assert.equal(calls.length, 3);
    assert.equal(calls[2][1], 'cancel_final_unit');
    handlers.confirm();
    assert.equal(calls.length, 3); // Rapid repeated confirmation is ignored.

    button.disabled = false;
    click(button);
    await settle();
    assert.equal(calls.length, 4);
    assert.equal(calls[3][1], 'remove_unit');
    assert.equal(shows, 2); // Ordinary quantity reduction does not open a modal.

    for (const action of ['add_unit', 'add_to_basket', 'remove_from_basket']) {
        button.disabled = false;
        button.dataset.basketAction = action;
        click(button);
        await settle();
        assert.deepEqual(calls.at(-1), ['42', action]);
    }
    assert.equal(calls.length, 7);
    assert.equal(shows, 2);
});

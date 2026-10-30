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

test('empty category/shop headings are hidden once every row beneath them is hidden', () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(
        template.indexOf('function toggleCancelledVisibility'),
        template.indexOf('// Keeps the visibility filter'),
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
            classList: makeClassList(['hide-cancelled']),
            children: rows.map(row => ({
                dataset: row.heading ? {groupHeading: row.heading} : {},
                classList: makeClassList(row.classes || []),
            })),
        };
    }

    let list, toggle;
    const context = vm.createContext({
        document: {
            getElementById: id => (
                id === 'shopping_list' ? list
                    : id === 'cancelled-toggle' ? toggle : null
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

test('bought filter initializes without toggling and follows collapse events', () => {
    const template = readFileSync(join(__dirname, 'templates/base.html'), 'utf8');
    const source = template.slice(template.indexOf('// Keeps the visibility filter'),
        template.indexOf('// All quantities, attribution'));
    const events = {}, sectionEvents = {};
    const toggle = {checked: true, addEventListener: (name, fn) => { events[name] = fn; }};
    let shown = false, constructions = 0, visibilitySyncs = 0;
    const section = {
        classList: {contains: name => name === 'show' && shown},
        addEventListener: (name, fn) => { sectionEvents[name] = fn; },
    };
    let instance;
    class Collapse {
        static getInstance(element) { return element === section ? instance : null; }
        constructor(element, options) {
            assert.equal(element, section);
            assert.equal(options.toggle, false);
            constructions++;
            instance = this;
        }
        show() { shown = true; sectionEvents['shown.bs.collapse'](); }
        hide() { shown = false; sectionEvents['hidden.bs.collapse'](); }
    }
    let initialize;
    const context = vm.createContext({
        bootstrap: {Collapse},
        document: {
            getElementById: id => id === 'bought-toggle' ? toggle : section,
            addEventListener: (name, fn) => { initialize = fn; },
        },
        toggleCancelledVisibility: () => { visibilitySyncs++; },
    });
    vm.runInContext(compatibilitySource() + source, context);
    initialize();
    assert.equal(shown, false);
    assert.equal(toggle.checked, false);
    assert.equal(visibilitySyncs, 1);
    toggle.checked = true;
    events.change();
    assert.equal(shown, true);
    toggle.checked = false;
    events.change();
    assert.equal(shown, false);
    instance.show();
    assert.equal(toggle.checked, true);
    instance.hide();
    assert.equal(toggle.checked, false);
    initialize();
    assert.equal(constructions, 1);
    context.document.getElementById = () => null;
    initialize(); // Other pages have no bought filter.
    assert.equal(constructions, 1);
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

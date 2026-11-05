// Run with: node --test shopping_list/tests_seed_selection.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const source = readFileSync(
    join(__dirname, '../static/shopping_list/seed_selection.js'), 'utf8'
);

function selection(initial = [[true, true], [true, true, true]]) {
    let initialize, change;
    const groups = initial.map(states => {
        const group = {};
        group.products = states.map(checked => ({
            checked,
            matches: () => false,
        }));
        group.checkbox = {
            checked: false, indeterminate: false,
            matches: selector => selector === '[data-seed-category]',
            closest: () => group,
        };
        group.querySelector = () => group.checkbox;
        group.querySelectorAll = () => group.products;
        return group;
    });
    const all = {checked: true, indeterminate: false};
    const count = {textContent: ''};
    const products = groups.flatMap(group => group.products);
    const form = {
        querySelector: () => all,
        querySelectorAll: selector => selector === '[data-seed-category-group]'
            ? groups : products,
        addEventListener: (event, handler) => { change = handler; },
    };
    const context = vm.createContext({
        document: {
            addEventListener: (event, handler) => { initialize = handler; },
            getElementById: id => id === 'seed-selection-form' ? form : count,
        },
    });
    vm.runInContext(source, context);
    initialize();
    return {
        groups, products, all, count,
        change: (checkbox, checked) => {
            checkbox.checked = checked;
            change({target: checkbox});
        },
    };
}

test('global checkbox selects and deselects every category and product', () => {
    const ui = selection();
    assert.equal(ui.count.textContent, 'Selected products: 5');
    assert.equal(ui.all.checked, true);
    assert.equal(ui.all.indeterminate, false);
    ui.change(ui.all, false);
    assert.ok(ui.products.every(product => !product.checked));
    assert.ok(ui.groups.every(group => !group.checkbox.checked));
    assert.equal(ui.all.indeterminate, false);
    assert.equal(ui.count.textContent, 'Selected products: 0');
    ui.change(ui.all, true);
    assert.ok(ui.products.every(product => product.checked));
    assert.ok(ui.groups.every(group => group.checkbox.checked));
    assert.equal(ui.count.textContent, 'Selected products: 5');
});

test('category checkbox updates its products and global mixed state only', () => {
    const ui = selection();
    ui.change(ui.groups[0].checkbox, false);
    assert.ok(ui.groups[0].products.every(product => !product.checked));
    assert.ok(ui.groups[1].products.every(product => product.checked));
    assert.equal(ui.all.checked, false);
    assert.equal(ui.all.indeterminate, true);
    assert.equal(ui.count.textContent, 'Selected products: 3');
    ui.change(ui.groups[1].checkbox, false);
    assert.equal(ui.all.indeterminate, false);
    ui.change(ui.groups[0].checkbox, true);
    assert.equal(ui.all.indeterminate, true);
    assert.equal(ui.groups[0].checkbox.indeterminate, false);
    ui.change(ui.groups[1].checkbox, true);
    assert.equal(ui.all.checked, true);
    assert.equal(ui.all.indeterminate, false);
});

test('individual products update category and global tri-state checkboxes', () => {
    const ui = selection([[false, false], [false, false, false]]);
    ui.change(ui.groups[0].products[0], true);
    assert.equal(ui.groups[0].checkbox.checked, false);
    assert.equal(ui.groups[0].checkbox.indeterminate, true);
    assert.equal(ui.all.indeterminate, true);
    ui.change(ui.groups[0].products[1], true);
    assert.equal(ui.groups[0].checkbox.checked, true);
    assert.equal(ui.groups[0].checkbox.indeterminate, false);
    ui.change(ui.groups[0].products[0], false);
    assert.equal(ui.groups[0].checkbox.indeterminate, true);
    ui.change(ui.groups[0].products[1], false);
    assert.equal(ui.groups[0].checkbox.indeterminate, false);
    assert.equal(ui.all.indeterminate, false);
    assert.equal(ui.count.textContent, 'Selected products: 0');
});

test('editing a partial selection restores mixed states on initialization', () => {
    const ui = selection([[true, false], [true, true, true]]);
    assert.equal(ui.groups[0].checkbox.indeterminate, true);
    assert.equal(ui.groups[1].checkbox.checked, true);
    assert.equal(ui.all.indeterminate, true);
    assert.equal(ui.count.textContent, 'Selected products: 4');
});

test('pages without a selection form do not initialize controls', () => {
    let initialize;
    vm.runInNewContext(source, {
        document: {
            addEventListener: (event, handler) => { initialize = handler; },
            getElementById: () => null,
        },
    });
    assert.doesNotThrow(() => initialize());
});

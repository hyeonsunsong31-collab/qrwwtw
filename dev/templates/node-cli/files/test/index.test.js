const test = require('node:test');
const assert = require('node:assert');
const { run } = require('../index.js');

test('prints its input', () => {
  assert.match(run(['hello']), /hello/);
});

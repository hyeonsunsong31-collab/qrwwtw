#!/usr/bin/env node
// {{TITLE}} -- command-line tool
function run(args) {
  return `{{TITLE}}: ready (input: ${args[0] || 'none'})`;
}
module.exports = { run };
if (require.main === module) console.log(run(process.argv.slice(2)));

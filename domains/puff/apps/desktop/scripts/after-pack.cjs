const { cp, mkdir, rm } = require('node:fs/promises');
const { join } = require('node:path');

module.exports = async function copyDshRuntime(context) {
  const source = process.env.PUFF_RUNTIME_DIR;
  if (!source) {
    throw new Error('PUFF_RUNTIME_DIR is required for afterPack');
  }
  const target = join(context.appOutDir, 'resources', 'dsh-runtime');

  await rm(target, { recursive: true, force: true });
  await mkdir(target, { recursive: true });
  await cp(source, target, { recursive: true, dereference: true });
};

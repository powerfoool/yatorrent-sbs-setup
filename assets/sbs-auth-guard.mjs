// Outside the job process group: remove only its temporary auth symlink on IPC EOF.
import fs from 'node:fs';
import path from 'node:path';

try {
  const args = process.argv.slice(2);
  if (args.length !== 1 || !process.connected || !path.isAbsolute(args[0])) throw new Error('Invalid cleanup guard invocation');
  const link = args[0], home = path.dirname(link);
  const directory = fs.lstatSync(home), original = fs.lstatSync(link);
  if (path.basename(link) !== 'auth.json' || !/^\.sbs-cli-[A-Za-z0-9]+$/.test(path.basename(home)) || !directory.isDirectory() || directory.isSymbolicLink() || directory.uid !== process.getuid() || (directory.mode & 0o077) || !original.isSymbolicLink() || original.uid !== process.getuid()) throw new Error('Unsafe temporary auth link');
  process.once('disconnect', () => {
    try {
      const currentDirectory = fs.lstatSync(home), current = fs.lstatSync(link);
      if (currentDirectory.dev !== directory.dev || currentDirectory.ino !== directory.ino || current.dev !== original.dev || current.ino !== original.ino || !current.isSymbolicLink()) throw new Error('Auth link identity changed');
      fs.unlinkSync(link);
      process.exit(0);
    } catch (error) {
      process.exit(error.code === 'ENOENT' ? 0 : 1);
    }
  });
  process.send({ready: true});
} catch {
  process.stderr.write('SBS auth cleanup guard unavailable\n');
  process.exit(1);
}

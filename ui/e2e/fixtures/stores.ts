import { execFileSync } from 'node:child_process';

/** The Compose project the stack runs as (`docker compose` in e2e/ names it after the folder). */
export const PROJECT = process.env.VT_COMPOSE_PROJECT ?? 'e2e';
export const container = (service: string): string => `${PROJECT}-${service}-1`;

const docker = (args: string[]): string => execFileSync('docker', args, { encoding: 'utf8', timeout: 30_000 }).trim();

/**
 * What the sinks hold, read and changed directly, the way someone with access to the store would: that is how a spec
 * makes drift (a value changed outside Git) and checks that an action really wrote, or did not write, anything.
 * Nothing here goes through varTrack.
 */
export class Stores {
  /** A redis-cli command on the stack's Redis, in database *db* (redis-hash is 2, redis-string 3). */
  redis(db: number, ...args: string[]): string {
    return docker(['exec', container('redis'), 'redis-cli', '-n', String(db), ...args]);
  }

  /** Keys matching a pattern, sorted. */
  redisKeys(db: number, pattern: string): string[] {
    return this.redis(db, '--scan', '--pattern', pattern).split('\n').filter(Boolean).sort();
  }

  /** The value of a string key; `field` reads a field of a hash instead. */
  redisGet(db: number, key: string, field?: string): string {
    return field ? this.redis(db, 'HGET', key, field) : this.redis(db, 'GET', key);
  }

  redisSet(db: number, key: string, value: string, field?: string): void {
    if (field) this.redis(db, 'HSET', key, field, value);
    else this.redis(db, 'SET', key, value);
  }

  redisDelete(db: number, ...keys: string[]): void {
    if (keys.length) this.redis(db, 'DEL', ...keys);
  }

  /** JavaScript for mongosh, run against the `vartrack` database; returns what it prints. */
  mongo(script: string): string {
    return docker(['exec', container('mongo'), 'mongosh', '--quiet', '--eval', `db = db.getSiblingDB("vartrack"); ${script}`]);
  }

  /**
   * One config value of an environment, where the Mongo sink keeps it. The document strategies store a file as one document
   * per environment, the config nested under `data` (so "api.rate_limit" is `data.api.rate_limit`); STRATEGY_KEY_VALUE, one
   * document per key (use `mongoKeyValue`).
   */
  mongoValue(collection: string, env: string, path: string): string {
    return this.mongo(
      `const d = db.getCollection(${JSON.stringify(collection)}).findOne({ _vt_env: ${JSON.stringify(env)} }); ` +
        `print(${JSON.stringify(path)}.split('.').reduce((o, k) => (o == null ? o : o[k]), d && d.data))`,
    );
  }

  /** Change one value of an environment's document, as someone with access to the database would. */
  mongoSet(collection: string, env: string, path: string, value: string): void {
    this.mongo(
      `db.getCollection(${JSON.stringify(collection)}).updateOne({ _vt_env: ${JSON.stringify(env)} }, ` +
        `{ $set: { ${JSON.stringify(`data.${path}`)}: ${JSON.stringify(value)} } })`,
    );
  }

  /** The value of one key's own document (STRATEGY_KEY_VALUE: `{_vt_env, key, value}`). */
  mongoKeyValue(collection: string, env: string, key: string): string {
    return this.mongo(
      `const d = db.getCollection(${JSON.stringify(collection)}).findOne({ _vt_env: ${JSON.stringify(env)}, key: ${JSON.stringify(key)} }); print(d ? d.value : '')`,
    );
  }

  mongoCount(collection: string, filter = '{}'): number {
    return Number(this.mongo(`print(db.getCollection(${JSON.stringify(collection)}).countDocuments(${filter}))`));
  }

  mongoDrop(collection: string): void {
    this.mongo(`db.getCollection(${JSON.stringify(collection)}).drop()`);
  }
}

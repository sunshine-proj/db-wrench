const e = encodeURIComponent;

export const explorerRoutes = {
  connection: (cid: string) => `/explorer/${e(cid)}`,
  schema: (cid: string, db: string, schema: string) =>
    `/explorer/${e(cid)}/s/${e(db)}/${e(schema)}`,
  schemaTable: (cid: string, db: string, schema: string, table: string) =>
    `/explorer/${e(cid)}/s/${e(db)}/${e(schema)}/${e(table)}`,
  table: (cid: string, db: string, table: string) => `/explorer/${e(cid)}/d/${e(db)}/${e(table)}`,
  collection: (cid: string, db: string, coll: string) =>
    `/explorer/${e(cid)}/m/${e(db)}/${e(coll)}`,
};

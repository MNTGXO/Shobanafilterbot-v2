// Run deliberately with an index-admin account against the intended catalog.
// Run once for each distinct DATABASE_URI{,2..5}/DATABASE_NAME{,2..5} pair.
const database = process.env.DATABASE_NAME;
const collection = process.env.COLLECTION_NAME;
if (!database || !collection) {
    throw new Error('Set DATABASE_NAME and COLLECTION_NAME explicitly before running this script.');
}
const target = db.getSiblingDB(database);
if (!target.getCollectionNames().includes(collection)) {
    throw new Error('Catalog collection not found; refusing to create it implicitly.');
}
print(target.getCollection(collection).createIndex(
    { file_name: 'text' },
    { name: 'shobana_file_name_text', default_language: 'none' }
));

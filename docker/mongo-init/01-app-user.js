// Cria o usuário da aplicação (só readWrite) no banco do projeto e no banco de testes.
// ATENÇÃO: este script roda somente na primeira inicialização do volume mongo_data.
// Para recriar o usuário, apague o volume (perde os dados locais): docker compose down -v

const dbName = process.env.MONGO_DB_NAME;
const appUser = process.env.MONGO_APP_USER;
const appPassword = process.env.MONGO_APP_PASSWORD;

if (!dbName || !appUser || !appPassword) {
  throw new Error("Defina MONGO_DB_NAME, MONGO_APP_USER e MONGO_APP_PASSWORD no .env");
}

db.getSiblingDB(dbName).createUser({
  user: appUser,
  pwd: appPassword,
  roles: [
    { role: "readWrite", db: dbName },
    { role: "readWrite", db: dbName + "_test" },
  ],
});

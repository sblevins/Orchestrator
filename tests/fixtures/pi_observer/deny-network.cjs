const fs = require("node:fs");
function deny() {
  fs.appendFileSync(process.env.OBSERVER_FIXTURE + "/network-attempts.log", "Blocked network attempt\n");
  throw new Error("Observer tests forbid network access");
}
globalThis.fetch = deny;
require("node:net").Socket.prototype.connect = deny;
require("node:http").request = deny;
require("node:http").get = deny;
require("node:https").request = deny;
require("node:https").get = deny;
require("node:tls").connect = deny;
require("node:module").syncBuiltinESMExports();

const dns = require('node:dns');
const nativeLookup = dns.lookup.bind(dns);

dns.lookup = function ipv4Lookup(hostname, options, callback) {
	if (typeof options === 'function') {
		callback = options;
		options = {};
	} else if (typeof options === 'number') {
		options = { family: options };
	}

	return nativeLookup(hostname, { ...options, family: 4 }, callback);
};

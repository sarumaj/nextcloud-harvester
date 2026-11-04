// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The sister app's vault code (Vaults for Cryptomator, js/vault.js) on a vault in a folder, for
// tests/test_vault_js.py: the other implementation that reads what harvester/vault.py writes
// and writes what it reads.
//
//   node tests/vault_js.mjs <sister repository> dump <vault> <password>
//     Every entry as JSON ({path: {type, size, sha256, target}}), the health check's findings,
//     and each directory's dirid.c9r decrypted and compared with its ID.
//   node tests/vault_js.mjs <sister repository> write <vault> <password> <spec.json>
//     Writes the spec's entries ([{path, file}] or [{path, dir: true}]), the folders along each
//     path made where missing.

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

const [repository, command, folder, password, specFile] = process.argv.slice(2)
const require = createRequire(path.join(repository, 'package.json'))
const { Vault } = require('./app/cryptomator_vaults/js/vault.js')
const { folderStorage } = require('./tests/storage.js')

const decoder = new TextDecoder('utf-8', { fatal: true })
const vault = await Vault.open(folderStorage(folder, { writable: true }))
await vault.unlock(password)

async function dump() {
	const entries = {}
	const dirIds = []
	const walk = async (dirId, prefix) => {
		dirIds.push(dirId)
		for (const entry of await vault.list(dirId)) {
			const at = prefix + entry.name
			if (entry.type === 'file') {
				const data = await vault.readFile(entry)
				entries[at] = { type: 'file', size: data.length, sha256: createHash('sha256').update(data).digest('hex') }
			} else if (entry.type === 'dir') {
				entries[at] = { type: 'dir' }
				await walk(entry.dirId, at + '/')
			} else {
				entries[at] = { type: entry.type, target: entry.target, error: entry.error }
			}
		}
	}
	await walk('', '')
	const dirIdBackups = {}
	for (const dirId of dirIds) {
		const backup = path.join(await vault.dirPath(dirId), 'dirid.c9r')
		try {
			const stored = decoder.decode(await vault.decryptContents(await vault.storage.read(backup)))
			dirIdBackups[dirId] = stored === dirId ? 'ok' : 'differs: ' + stored
		} catch (error) {
			dirIdBackups[dirId] = 'unreadable: ' + error.message
		}
	}
	return { entries, health: await vault.checkHealth(), dirIdBackups }
}

async function write() {
	for (const item of JSON.parse(fs.readFileSync(specFile, 'utf8'))) {
		const names = item.path.split('/')
		const last = item.dir ? names.length : names.length - 1
		let dirId = ''
		for (const name of names.slice(0, last)) {
			dirId = (await vault.ensureDirectory(dirId, name)).dirId
		}
		if (!item.dir) {
			await vault.writeFile(dirId, names[names.length - 1], new Uint8Array(fs.readFileSync(item.file)))
		}
	}
	return { written: true }
}

const result = command === 'dump' ? await dump() : await write()
process.stdout.write(JSON.stringify(result))

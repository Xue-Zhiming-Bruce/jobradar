import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Every backend call lives in `src/api/client.ts`, and this scan is what holds
 * that: it reads every `.ts`/`.tsx` file under `src/` -- the mock backend and
 * the tests aside -- and names the files that reach the network.
 *
 * Vitest runs from `frontend/`, so the repository root is one directory up.
 */
const repoRoot = join(process.cwd(), '..')
const srcDir = join(process.cwd(), 'src')
const clientFile = 'frontend/src/api/client.ts'
const httpCall = /fetch\(|axios|XMLHttpRequest/

function sources(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    // The mock backend is not app code: it answers requests, it never makes one.
    if (entry.name === 'mocks') return []

    const path = join(directory, entry.name)
    if (entry.isDirectory()) return sources(path)
    if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return []

    return [relative(repoRoot, path)]
  })
}

describe('the scan', () => {
  const files = sources(srcDir).sort()

  it('reads the app sources, so a broken glob cannot pass', () => {
    expect(files.length).toBeGreaterThan(0)
    expect(files).toContain(clientFile)
  })

  it('finds the API module and no other file that reaches the network', () => {
    const calling = files.filter((file) => httpCall.test(readFileSync(join(repoRoot, file), 'utf8')))

    // The client is the one module allowed to call; anything else here is stray.
    expect(calling).toEqual([clientFile])
  })
})

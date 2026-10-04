# Prixville projects

This repo is the Prixville phone swipe feed: one HTML page of upcoming and live NFT projects. Drag a card right to like it and left to dislike it. A short tap still opens the social links. Likes play a water splash, a squish sound, and a vibration when the phone allows it. Dislikes show a big red X and play a loud buzz. Audio stays silent until the first tap. Each card shows view, like, and dislike counts, the Prixville type (Rokkitt, Crimson Text, JetBrains Mono), and the wallet color already stored for that project (green, yellow, or red).

The page lists the seven projects that were already in the file. It does not add projects, prices, wallets, follower counts, or X posts.

## Run the server

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Then open http://127.0.0.1:8000

`server.py` uses the Python standard library plus `eth-account` (to check wallet signatures) and `sqlite3` (to store votes). The database file is `votes.sqlite3` and is gitignored.

If you open `index.html` as a plain file, swipes still work on that device, but the page says the vote was not saved. A downloaded file cannot see other wallets, so those swipes are not a real vote.

## Vote rule

A vote is a connected-wallet `personal_sign` of a clear message that includes the project id and either like or dislike:

```text
Prixville vote
Project: <project id>
Vote: like
```

or `Vote: dislike`. The server checks that signature and rejects a bad one.

One vote per lowercase address per project. A second vote from the same address is rejected.

Before accepting a vote, the server looks up public transaction history for that address on Ethereum (https://eth.blockscout.com/api/v2) and Base (https://base.blockscout.com/api/v2). It reads only the first page of results those APIs actually return: normal transactions (the newest page and the oldest page), internal transactions, and token transfers. It treats the address as the same person as any address it has sent to or received from on those pages, and as the address that first funded it when that incoming transfer appears on the oldest page. If any of those addresses already voted on that same project, the vote is rejected and the server says it is tied to a wallet that already voted. If the API fails, the server does not pretend it checked: it rejects the vote with "could not check related wallets" and does not count it. There is no private blacklist, and the server does not invent links between wallets.

A vote counts only if that wallet currently holds at least one token from a collection whose contract was checked on a public page:

- Prixville Ticket, ERC-721 `0x391c31b74fb6824ed22a59ee325c0ecaf3bbdcc8`. Etherscan's token tracker names it "Prixville Ticket (prixxxxx)", and Blockscout lists the same name, symbol, and ERC-721 type (supply 100): https://etherscan.io/token/0x391c31b74fb6824ed22a59ee325c0ecaf3bbdcc8 and https://eth.blockscout.com/token/0x391C31B74fB6824ed22a59eE325c0eCAF3bBDCc8
- Prixville NFT, ERC-721 `0x22115e975da96f3e3eb33771869d387773bd286c`. prixville.com links to https://opensea.io/collection/prixville, and OpenSea's collection API lists this contract. On-chain the token name is "Prix" (symbol Tuskss, supply 1000): https://eth.blockscout.com/token/0x22115e975Da96f3E3eb33771869D387773bd286C
- The Richards, ERC-721 `0xf054daf83ec676d9a1e85ce9d1a6d5a65e8e75c1`. OpenSea collection https://opensea.io/collection/the-richardss lists this contract and sets the project URL to https://prixville.com. Blockscout names it "The Richards" (symbol PRXX, supply 505): https://eth.blockscout.com/token/0xF054DAf83EC676d9A1e85cE9D1A6D5a65e8E75C1

The server calls ERC-721 `balanceOf` on each of those contracts through a public Ethereum RPC. If the balance is at least 1 on any of them, the holding check passes. If every balance is 0, the vote is rejected with "This wallet does not hold a Prixville ticket, Prixville NFT, or a Richards" and is not counted. If the RPC does not return a balance, the vote is rejected with "could not check Prixville holdings" and is not counted. The one-vote-per-address rule and the affiliated-wallet rejection still apply after that.

prixville.com also links to https://opensea.io/collection/pville (titled P'ville Ticket on OpenSea, supply 100). That page's HTML did not include a contract address, and the OpenSea API for that slug returned unauthorized, so no separate address for the pville slug is used. It is not assumed to be the ticket contract above.

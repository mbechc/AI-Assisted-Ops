# Steward terms of use

Terms version 1

By installing or using Steward you accept the following.

1. **Not a Cisco product.** Steward is an independent, personal open-source project. It is not a Cisco
   product, and it is not affiliated with, endorsed by, certified by or supported by Cisco Systems, Inc.
   Cisco TAC does not support Steward. That the author works for Cisco does not change this.

2. **No warranty, no liability.** Steward is provided "as is", without warranty of any kind and without any
   liability for damages, as set out in sections 7 and 8 of the Apache License 2.0 under which it is
   distributed (see `LICENSE`). Neither the author nor Cisco is liable for downtime, misconfiguration, data
   loss or any other consequence of using Steward.

3. **It can change your network.** Given a Catalyst Center account with write permission, Steward can make
   changes through the Catalyst Center API, automatically at autonomy levels L2 and L3. You are responsible
   for testing Steward, for your change control, and for every change made with it.

4. **Read-only assessment.** To evaluate Steward without any risk of change, give it a Catalyst Center user
   with the read-only `OBSERVER-ROLE` (see `docs/READ_ONLY.md`). Catalyst Center then refuses every change.
   No setting inside Steward, including the kill switch and the autonomy caps, is a substitute for that.

5. **Trademarks.** Cisco, Catalyst Center and related names are trademarks of Cisco Systems, Inc. They are
   used only to describe what Steward works with.

If you do not accept these terms, do not install or use Steward.

# sonic-kne-gcp

SONiC virtual switches on KNE (Kubernetes Network Emulation), running on a single GCP VM, with an Ixia-c traffic generator.

- [GCP_VM_FROM_SCRATCH.md](GCP_VM_FROM_SCRATCH.md): everything from creating the VM to sending traffic through a leaf-spine fabric.
- [topology/leaf-spine-otg.pb.txt](topology/leaf-spine-otg.pb.txt): the KNE topology (2 leaf, 1 spine, Ixia-c with 2 ports).
- [scripts/otg_test.py](scripts/otg_test.py): the snappi traffic test.

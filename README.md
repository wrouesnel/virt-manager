# Virtual Machine Manager (wrouesnel fork)

This is a personal fork of [virt-manager](https://github.com/virt-manager/virt-manager)
carrying patches that improve the serial console, terminal resize propagation,
and other quality-of-life features not yet upstream.

**`main` is a leading-edge integration branch.** It tracks upstream `main` with
our patches rebased on top. Tags of the form `v5.1.0+wrouesnel.N` mark packaged
releases. Do not force-push `main` without rebasing the packaging repo's
`upstream.env` to match.

Packages are built by the
[virt-manager-packaging](https://github.com/wrouesnel/virt-manager-packaging) repo
and published to the Launchpad PPA
[`ppa:w-rouesnel/virt-manager`](https://launchpad.net/~w-rouesnel/+archive/ubuntu/virt-manager).

## Patches on top of upstream

| Patch | Description |
|---|---|
| serialcon: TIOCSWINSZ | Set host PTY window size on VTE resize (future QEMU char-pty support) |
| serialcon: guest agent resize | Fallback: run `stty` via guest agent for current QEMU |
| serialcon: manual resize menu | Right-click "Set Terminal Size" sends `stty` command for guests without the agent |

## Upstream README

`virt-manager` is a graphical tool for managing virtual machines
via [libvirt](https://libvirt.org). Most usage is with QEMU/KVM
virtual machines, but Xen and libvirt LXC containers are well
supported. Common operations for any libvirt driver should work.

Several command line tools are also provided:

 - `virt-install`: Create new libvirt virtual machines
 - `virt-clone`: Duplicate existing libvirt virtual machines
 - `virt-xml`: Edit existing libvirt virtual machines/manipulate libvirt XML

For dependency info and installation instructions, see the
[INSTALL.md](INSTALL.md) file. If you just want to quickly test the
code from a git checkout, you can launch any of the commands like:

```sh
./virt-manager --debug ...
```

## Contact

 - For IRC we use #virt on OFTC.
 - For bug reporting info, see
   [virt-manager bug reporting](https://virt-manager.org/bugs).
 - There are further project details on the
   [virt-manager](https://virt-manager.org/) website.
 - See the [CONTRIBUTING.md](CONTRIBUTING.md) file for info about submitting patches or
   contributing translations.

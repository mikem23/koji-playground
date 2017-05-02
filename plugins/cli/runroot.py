def handle_runroot(options, session, args):
    "[admin] Run a command in a buildroot"
    usage = _("usage: %prog runroot [options] <tag> <arch> <command>")
    usage += _("\n(Specify the --help global option for a list of other help options)")
    parser = OptionParser(usage=usage)
    parser.disable_interspersed_args()
    parser.add_option("-p", "--package", action="append", default=[], help=_("make sure this package is in the chroot"))
    parser.add_option("-m", "--mount", action="append", default=[], help=_("mount this directory read-write in the chroot"))
    parser.add_option("--skip-setarch", action="store_true", default=False,
            help=_("bypass normal setarch in the chroot"))
    parser.add_option("-w", "--weight", type='int', help=_("set task weight"))
    parser.add_option("--channel-override", help=_("use a non-standard channel"))
    parser.add_option("--task-id", action="store_true", default=False,
            help=_("Print the ID of the runroot task"))
    parser.add_option("--use-shell", action="store_true", default=False,
            help=_("Run command through a shell, otherwise uses exec"))
    parser.add_option("--new-chroot", action="store_true", default=False,
            help=_("Run command with the --new-chroot (systemd-nspawn) option to mock"))
    parser.add_option("--repo-id", type="int", help=_("ID of the repo to use"))

    (opts, args) = parser.parse_args(args)

    if len(args) < 3:
        parser.error(_("Incorrect number of arguments"))
    activate_session(session)
    tag = args[0]
    arch = args[1]
    if opts.use_shell:
        # everything must be correctly quoted
        command = ' '.join(args[2:])
    else:
        command = args[2:]
    try:
        kwargs = { 'channel':       opts.channel_override,
                   'packages':      opts.package,
                   'mounts':        opts.mount,
                   'repo_id':       opts.repo_id,
                   'skip_setarch':  opts.skip_setarch,
                   'weight':        opts.weight }
        # Only pass this kwarg if it is true - this prevents confusing older
        # builders with a different function signature
        if opts.new_chroot:
            kwargs['new_chroot'] = True

        task_id = session.runroot(tag, arch, command, **kwargs)
    except koji.GenericError, e:
        if 'Invalid method' in str(e):
            print("* The runroot plugin appears to not be installed on the"
                  " koji hub.  Please contact the administrator.")
        raise
    if opts.task_id:
        print(task_id)

    try:
        while True:
            # wait for the task to finish
            if session.taskFinished(task_id):
                break
            time.sleep(options.poll_interval)
    except KeyboardInterrupt:
        # this is probably the right thing to do here
        print("User interrupt: canceling runroot task")
        session.cancelTask(task_id)
        raise
    output = list_task_output_all_volumes(session, task_id)
    if 'runroot.log' in output:
        for volume in output['runroot.log']:
            log = session.downloadTaskOutput(task_id, 'runroot.log', volume=volume)
            sys.stdout.write(log)
    info = session.getTaskInfo(task_id)
    if info is None:
        sys.exit(1)
    state = koji.TASK_STATES[info['state']]
    if state in ('FAILED', 'CANCELED'):
        sys.exit(1)
    return

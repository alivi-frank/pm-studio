"""Signal adapters. Each turns one kind of activity record into `Signal`s; the ledger
runs them and never knows which is which. Add a source by implementing
`base.SignalSource` and registering it in `ledger.build_sources`."""

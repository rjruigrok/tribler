from Tribler.Core.TorrentDef import TorrentDef
from Tribler.Core.DownloadConfig import DownloadStartupConfig
from Tribler.Core.Session import Session
from Tribler.Main.Dialogs.CreateTorrent import create_anon_torrent

torrentfile = "../tribler/anon_test.torrent"
#torrentfile = None
#torrentfile = "../tribler/c1d9e84156f036ed3cc017c92ccbe2d4c5c75466.torrent"
#torrentfile = "9e3a8f152da0d504e0112a03b00df70d49544991.torrent"
tdef = TorrentDef()
anontorrentfile = create_anon_torrent(tdef,torrentfile)

session = Session()
session.start()
tdef =  TorrentDef.load(anontorrentfile)
dscfg = DownloadStartupConfig()
dscfg.set_dest_dir("tmpdownload")
dscfg.set_anon_mode(True)
session.start_download(tdef,dscfg)
$a = Get-NetAdapter | Where-Object {$_.InterfaceDescription -like '*Remote NDIS*'} | Select-Object -First 1
if ($a) { Get-NetIPAddress -InterfaceIndex $a.InterfaceIndex -AddressFamily IPv4 | Select-Object -First 1 -ExpandProperty IPAddress }
